"""A libguestfs session on one disk image: mounting, file edits, scripts run in the guest root."""
from __future__ import annotations

import os
import shlex

import guestfs

from .refs import edit_grubenv
from .util import MigrateError, log, setting

GUEST_DIR = "/var/lib/petasus-migrate"
# The scripts' directory is memory of the appliance, not space of the guest's disk.
APPLIANCE_MEMORY_MB = 8192
SCRATCH_MB = 4096
BACKUP_DIR = "/var/backups/petasus-migrate"
REPORT = f"{GUEST_DIR}/report.tsv"


class Guest:
    """The disk is /dev/sda inside the appliance; extra drives follow as sdb, sdc."""

    disk = "/dev/sda"

    def __init__(self, path, *, fmt="qcow2", readonly=False, network=False, extra=()):
        os.environ.setdefault("LIBGUESTFS_BACKEND", "direct")
        g = guestfs.GuestFS(python_return_dict=True)
        g.set_memsize(APPLIANCE_MEMORY_MB)
        g.set_smp(min(4, os.cpu_count() or 1))
        g.set_network(network)
        g.add_drive_opts(path, format=fmt, readonly=readonly)
        for drive in extra:
            g.add_drive_opts(drive["path"], format=drive.get("format", "raw"), readonly=drive.get("readonly", True))
        g.launch()
        self.g = g
        self.readonly = readonly
        self.root = None
        self.notes = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close(sync=exc_type is None)

    def close(self, sync=True):
        if self.closed:
            return
        self.closed = True
        g = self.g
        try:
            if sync and not self.readonly:
                g.umount_all()
                g.shutdown()
        finally:
            g.close()

    # mounting

    def find_root(self) -> str:
        roots = self.g.inspect_os()
        if not roots:
            raise MigrateError("no operating system found on the disk")
        if len(roots) > 1:
            raise MigrateError(f"{len(roots)} operating systems found on the disk; one is supported")
        self.root = roots[0]
        return self.root

    def mount_os(self) -> dict:
        """Mount the guest as its fstab says. Returns {mountpoint: error} for the mounts that failed."""
        root = self.root or self.find_root()
        failed = {}
        mounts = self.g.inspect_get_mountpoints(root)
        for mountpoint in sorted(mounts, key=len):
            device = mounts[mountpoint]
            try:
                if self.readonly:
                    self.g.mount_ro(device, mountpoint)
                else:
                    self.g.mount(device, mountpoint)
            except RuntimeError as err:
                if mountpoint == "/":
                    raise MigrateError(f"the root filesystem {device} does not mount: {err}") from None
                failed[mountpoint] = str(err)
        return failed

    # files

    def exists(self, path: str) -> bool:
        return self.g.exists(path)

    def is_file(self, path: str) -> bool:
        return self.g.is_file(path, followsymlinks=True)

    def is_dir(self, path: str) -> bool:
        return self.g.is_dir(path, followsymlinks=True)

    def read(self, path: str, default: str | None = None) -> str | None:
        if not self.is_file(path):
            return default
        return self.g.read_file(path).decode("utf-8", errors="surrogateescape")

    def write(self, path: str, content, mode: int | None = None) -> None:
        """Replace a file's content in place, keeping its mode and owner when it exists."""
        data = content.encode("utf-8", errors="surrogateescape") if isinstance(content, str) else content
        g = self.g
        target = g.realpath(path) if g.exists(path) else path
        stat = g.statns(target) if g.exists(target) else None
        parent = os.path.dirname(target)
        if not g.is_dir(parent, followsymlinks=True):
            g.mkdir_p(parent)
        g.write(target, data)
        if stat is None:
            g.chmod(0o644 if mode is None else mode, target)
        else:
            g.chmod(stat["st_mode"] & 0o7777, target)
            g.chown(stat["st_uid"], stat["st_gid"], target)

    def edit(self, path: str, transform) -> bool:
        """Apply transform(text) -> text to a file. True when the content changed."""
        old = self.read(path)
        if old is None:
            return False
        new = transform(old)
        if new == old:
            return False
        self.write(path, new)
        return True

    def glob(self, pattern: str) -> list:
        return sorted(path.rstrip("/") for path in self.g.glob_expand(pattern))

    def paths(self, patterns) -> list:
        """The files the patterns name: a pattern with `*` expands, a plain path stands for itself."""
        return [path for pattern in patterns for path in (self.glob(pattern) if "*" in pattern else [pattern])]

    def edit_grubenv(self, path: str, transform) -> bool:
        """Apply transform(text) to a GRUB environment block. True when it changed."""
        if not self.is_file(path) or self.g.is_symlink(path):
            return False
        data = self.g.read_file(path)
        new = edit_grubenv(data, transform)
        if new == data:
            return False
        self.write(path, new)
        return True

    def backup_move(self, path: str) -> str:
        """Move a file or directory out of the way, keeping its path under the backup directory."""
        dest = f"{BACKUP_DIR}{path}"
        self.g.mkdir_p(os.path.dirname(dest))
        if self.g.exists(dest):
            self.g.rm_rf(dest)
        self.g.mv(path, dest)
        return dest

    def backup_copy(self, path: str) -> str:
        dest = f"{BACKUP_DIR}{path}"
        if not self.g.exists(dest):
            self.g.mkdir_p(os.path.dirname(dest))
            self.g.cp_a(path, dest)
        return dest

    # commands

    def install_scripts(self, source_dir: str, env: dict) -> None:
        """Put the guest scripts and their settings into a scratch directory in the guest's root.

        The directory is a tmpfs: scripts, package caches and temporary files of the conversion take
        no space on the guest's filesystems, which may be nearly full.
        """
        g = self.g
        self.remove_scripts()
        g.mkdir_p(GUEST_DIR)
        g.sh(f"mount -t tmpfs -o size={SCRATCH_MB}m,mode=0700 tmpfs {GUEST_DIR}")
        for name in sorted(os.listdir(source_dir)):
            g.upload(os.path.join(source_dir, name), f"{GUEST_DIR}/{name}")
            g.chmod(0o755, f"{GUEST_DIR}/{name}")
        env = dict(env, PM_DIR=GUEST_DIR, BACKUP=BACKUP_DIR)
        lines = [f"{key}={shlex.quote(str(value))}" for key, value in sorted(env.items())]
        g.write(f"{GUEST_DIR}/env", ("\n".join(lines) + "\n").encode())
        g.write(REPORT, b"")

    def upload(self, local: str, remote: str, mode: int = 0o644) -> None:
        self.g.mkdir_p(os.path.dirname(remote))
        self.g.upload(local, remote)
        self.g.chmod(mode, remote)

    def run_script(self, name: str) -> None:
        """Run one guest script, with a time limit; its output goes to our log when it ends."""
        logfile = f"{GUEST_DIR}/{name}.log"
        limit = int(setting("GUEST_STEP_TIMEOUT", "3600"))
        log(f"  running {name} in the guest")
        status = self.g.sh(
            f"cd {GUEST_DIR} && timeout -k 60 {limit} sh ./{name} >{logfile} 2>&1; echo $?"
        ).strip()
        output = self.read(logfile, "") or ""
        for line in output.splitlines():
            log(f"  {name}: {line}")
        self.collect_notes()
        if status in ("124", "137"):
            raise MigrateError(f"guest step {name} did not finish within {limit} s")
        if status != "0":
            fails = [note["message"] for note in self.notes if note["level"] == "FAIL"]
            reason = fails[-1] if fails else (output.strip().splitlines() or ["no output"])[-1]
            raise MigrateError(f"guest step {name} failed: {reason}")

    def collect_notes(self) -> None:
        text = self.read(REPORT, "") or ""
        if text:
            self.g.write(REPORT, b"")
        for line in text.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                self.note(*parts)

    def note(self, level: str, key: str, message: str) -> None:
        self.notes.append({"level": level, "key": key, "message": message})
        if level != "INFO":
            log(f"  {level} {key}: {message}")

    def remove_scripts(self) -> None:
        g = self.g
        if not g.is_dir(GUEST_DIR):
            return
        g.sh(f"if grep -qs ' {GUEST_DIR} ' /proc/mounts; then umount {GUEST_DIR} || umount -l {GUEST_DIR}; fi")
        g.rm_rf(GUEST_DIR)
