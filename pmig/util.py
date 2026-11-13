"""Shared helpers: logging, subprocesses, architecture names, locking, JSON files."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time


class MigrateError(Exception):
    """A step failed; the message says what and why."""


class Refused(MigrateError):
    """The source disk is outside what the tool converts."""

    def __init__(self, reasons):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


# Names that differ per CPU architecture. `platform` is the image API's arch_type.
ARCHES = {
    "x86_64": {
        "deb": "amd64",
        "efi": "x64",
        "boot_efi": "BOOTX64.EFI",
        "grub_target": "x86_64-efi",
        "console": "ttyS0",
        "platform": "amd64",
        "qemu": ["/usr/libexec/qemu-kvm", "qemu-system-x86_64"],
        "machine": "q35",
        "ovmf_code": [
            "/usr/share/edk2/ovmf/OVMF_CODE.secboot.fd",
            "/usr/share/OVMF/OVMF_CODE_4M.secboot.fd",
            "/usr/share/OVMF/OVMF_CODE.secboot.fd",
        ],
        "ovmf_vars": [
            "/usr/share/edk2/ovmf/OVMF_VARS.fd",
            "/usr/share/OVMF/OVMF_VARS_4M.fd",
            "/usr/share/OVMF/OVMF_VARS.fd",
        ],
    },
    "aarch64": {
        "deb": "arm64",
        "efi": "aa64",
        "boot_efi": "BOOTAA64.EFI",
        "grub_target": "arm64-efi",
        "console": "ttyAMA0",
        "platform": "arm64",
        "qemu": ["/usr/libexec/qemu-kvm", "qemu-system-aarch64"],
        "machine": "virt",
        "ovmf_code": [
            "/usr/share/edk2/aarch64/QEMU_EFI-silent-pflash.raw",
            "/usr/share/edk2/aarch64/QEMU_EFI-pflash.raw",
            "/usr/share/AAVMF/AAVMF_CODE.fd",
        ],
        "ovmf_vars": [
            "/usr/share/edk2/aarch64/vars-template-pflash.raw",
            "/usr/share/AAVMF/AAVMF_VARS.fd",
        ],
    },
}


def setting(name: str, default: str = "") -> str:
    """A setting from the environment. make exports an unset variable as an empty string: that is unset too."""
    return os.environ.get(name) or default


def host_arch() -> str:
    return platform.machine()


def arch_info(arch: str) -> dict:
    try:
        return ARCHES[arch]
    except KeyError:
        raise MigrateError(f"unsupported architecture {arch}") from None


def first_existing(paths, what: str) -> str:
    for path in paths:
        found = path if os.path.isabs(path) else shutil.which(path)
        if found and os.path.exists(found):
            return found
    raise MigrateError(f"{what} not found (looked for {', '.join(paths)})")


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def run(cmd, *, check=True, capture=False, **kwargs) -> subprocess.CompletedProcess:
    """Run a command; a failure raises MigrateError carrying its stderr."""
    if capture:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    kwargs.setdefault("text", True)
    try:
        proc = subprocess.run(cmd, **kwargs)
    except FileNotFoundError:
        raise MigrateError(f"{cmd[0]} is not installed (make deps)") from None
    if check and proc.returncode != 0:
        detail = (proc.stderr or "").strip() if capture else ""
        raise MigrateError(f"{' '.join(map(str, cmd))} failed ({proc.returncode})" + (f": {detail}" if detail else ""))
    return proc


def need(*tools: str) -> None:
    missing = [tool for tool in tools if not shutil.which(tool)]
    if missing:
        raise MigrateError(f"missing host tools: {', '.join(missing)} (make deps)")


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(4 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: str, data) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def qemu_img_info(path: str, fmt: str | None = None) -> dict:
    cmd = ["qemu-img", "info", "--output=json"]
    if fmt:
        cmd += ["-f", fmt]
    return json.loads(run(cmd + [path], capture=True).stdout)


def free_bytes(path: str) -> int:
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


def gib_ceil(size_bytes: int) -> int:
    return -(-size_bytes // (1 << 30))


@contextlib.contextmanager
def locked(directory: str):
    """One run per output directory."""
    os.makedirs(directory, exist_ok=True)
    handle = open(os.path.join(directory, ".lock"), "w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MigrateError(f"another run holds {directory}") from None
        yield
    finally:
        handle.close()
