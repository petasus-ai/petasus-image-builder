"""Files the conversion puts into a guest that the guest's repositories do not carry."""
from __future__ import annotations

import fcntl
import hashlib
import os
import tarfile
import urllib.request

from .util import MigrateError, arch_info, log, setting

NODE_EXPORTER_VERSION = "1.11.1"
RELEASES = "https://github.com/prometheus/node_exporter/releases/download"


def _fetch(url: str, dest: str) -> None:
    tmp = f"{dest}.part"
    try:
        with urllib.request.urlopen(url, timeout=60) as response, open(tmp, "wb") as handle:
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                handle.write(block)
    except OSError as err:
        raise MigrateError(f"cannot download {url}: {err}") from None
    os.replace(tmp, dest)


def node_exporter(cache_dir: str, arch: str) -> str:
    """Path of the node_exporter binary for arch: the official release, checked against its checksum list.

    NODE_EXPORTER_TARBALL names a release tarball already on this host, for a build without Internet access.
    """
    version = setting("NODE_EXPORTER_VERSION", NODE_EXPORTER_VERSION)
    name = f"node_exporter-{version}.linux-{arch_info(arch)['platform']}"
    binary = os.path.join(cache_dir, name, "node_exporter")
    if os.path.isfile(binary):
        return binary
    os.makedirs(cache_dir, exist_ok=True)
    # Conversions of other names share the cache: one fills it, the others wait.
    with open(os.path.join(cache_dir, f"{name}.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not os.path.isfile(binary):
            _populate(cache_dir, version, name, binary)
    return binary


def _populate(cache_dir: str, version: str, name: str, binary: str) -> None:
    tarball = setting("NODE_EXPORTER_TARBALL")
    if not tarball:
        tarball = os.path.join(cache_dir, f"{name}.tar.gz")
        sums = os.path.join(cache_dir, f"node_exporter-{version}.sha256sums.txt")
        log(f"downloading node_exporter {version}")
        _fetch(f"{RELEASES}/v{version}/sha256sums.txt", sums)
        _fetch(f"{RELEASES}/v{version}/{name}.tar.gz", tarball)
        expected = None
        with open(sums, encoding="utf-8") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) == 2 and fields[1] == f"{name}.tar.gz":
                    expected = fields[0]
        digest = hashlib.sha256()
        with open(tarball, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        if expected is None or digest.hexdigest() != expected:
            os.unlink(tarball)
            raise MigrateError(f"{name}.tar.gz does not match the release's checksum list")
    with tarfile.open(tarball) as archive:
        member = archive.getmember(f"{name}/node_exporter")
        os.makedirs(os.path.dirname(binary), exist_ok=True)
        source = archive.extractfile(member)
        if source is None:
            raise MigrateError(f"{tarball} holds no node_exporter binary")
        with source, open(f"{binary}.part", "wb") as handle:
            handle.write(source.read())
    os.chmod(f"{binary}.part", 0o755)
    os.replace(f"{binary}.part", binary)
