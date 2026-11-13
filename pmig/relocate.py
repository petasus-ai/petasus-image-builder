"""Give a disk without an EFI system partition one, at the front of a new GPT disk.

The platform boots UEFI only. The partitions of the source are copied byte for byte behind a new
ESP, so filesystem UUIDs and LVM volumes are unchanged, partition numbers are kept, and the last
partition still has the end of the disk to grow into.
"""
from __future__ import annotations

import os
import re

from .guest import Guest
from .inspect import ESP_GUID
from .util import MigrateError, log, run

MIB = 1 << 20
ESP_BYTES = 256 * MIB
SECTOR = 512
GPT_SECTORS = 34  # protective MBR + header + entries at the front; header + entries at the back
# UEFI firmware tells FAT32 from FAT16 by the cluster count alone: below this it misreads the volume.
FAT32_MIN_CLUSTERS = 65525
ESP_CLUSTER_SECTORS = 4


def plan_layout(disk: dict, esp_bytes: int = ESP_BYTES, esp_source: int | None = None) -> dict:
    """Where every partition lands on the new disk. Offsets are bytes; `end` is inclusive.

    esp_source names a FAT partition the guest already mounts at /boot/efi: it becomes the EFI
    system partition, and no new one is added.
    """
    if disk["sector_size"] != SECTOR:
        raise MigrateError(f"the disk has {disk['sector_size']}-byte sectors; only 512 is supported")
    parts = [part for part in disk["partitions"] if part["kind"] != "extended"]
    if not parts:
        raise MigrateError("the disk has no partitions to carry over")
    if max(part["num"] for part in parts) >= 128:
        raise MigrateError("the partition table has no free entry for an EFI system partition")
    first = min(part["start"] for part in parts)
    # Everything moves by one constant, so the partitions keep their alignment to each other.
    room = 0 if esp_source else esp_bytes
    shift = -(-(room + max(0, MIB - first)) // MIB) * MIB
    layout = []
    for part in sorted(parts, key=lambda part: part["num"]):
        layout.append({
            "num": part["num"],
            "source": part["num"],
            "start": part["start"] + shift,
            "end": part["end"] + shift,
            "type": ESP_GUID if part["num"] == esp_source else part["type"],
            "name": part.get("name") or "",
            "guid": part.get("guid"),
            "attributes": part.get("attributes", 0),
        })
    ordered = sorted(layout, key=lambda entry: entry["start"])
    for before, after in zip(ordered, ordered[1:]):
        if before["end"] >= after["start"]:
            raise MigrateError(f"partitions {before['num']} and {after['num']} overlap")
    if esp_source:
        esp = next(entry for entry in layout if entry["num"] == esp_source)
        partitions = layout
    else:
        esp = {
            "num": max(entry["num"] for entry in layout) + 1,
            "source": None,
            "start": MIB,
            "end": MIB + esp_bytes - 1,
            "type": ESP_GUID,
            "name": "EFI System Partition",
            "guid": None,
            "attributes": 0,
        }
        if esp["end"] >= ordered[0]["start"]:
            raise MigrateError("the EFI system partition does not fit before the first partition")
        partitions = layout + [esp]
    last_end = max(entry["end"] for entry in layout)
    size = -(-max(disk["size"] + shift, last_end + 1 + GPT_SECTORS * SECTOR) // MIB) * MIB + MIB
    return {"size": size, "shift": shift, "partitions": partitions, "esp": esp, "disk_guid": disk.get("disk_guid")}


def sgdisk_args(layout: dict) -> list:
    args = ["--clear", "--set-alignment=1"]
    if layout["disk_guid"]:
        args.append(f"--disk-guid={layout['disk_guid']}")
    for entry in layout["partitions"]:
        num = entry["num"]
        args.append(f"--new={num}:{entry['start'] // SECTOR}:{entry['end'] // SECTOR}")
        args.append(f"--typecode={num}:{entry['type']}")
        if entry["guid"]:
            args.append(f"--partition-guid={num}:{entry['guid']}")
        if entry["name"]:
            args.append(f"--change-name={num}:{entry['name']}")
        for bit in range(64):
            if entry["attributes"] >> bit & 1:
                args.append(f"--attributes={num}:set:{bit}")
    return args


def relocate(source: str, source_format: str, dest: str, disk: dict, esp_source: int | None = None) -> dict:
    """Write the relocated disk to dest, a new raw file. Returns the layout."""
    layout = plan_layout(disk, esp_source=esp_source)
    if os.path.exists(dest):
        os.unlink(dest)
    with open(dest, "wb") as handle:
        handle.truncate(layout["size"])
    run(["sgdisk"] + sgdisk_args(layout) + [dest], capture=True)
    run(["sgdisk", "--verify", dest], capture=True)
    esp = layout["esp"]
    if esp["source"] is None:
        made = run(["mkfs.vfat", "-v", "-F", "32", "-s", str(ESP_CLUSTER_SECTORS), "-n", "EFI", "-S", str(SECTOR),
                    "--offset", str(esp["start"] // SECTOR), dest, str((esp["end"] + 1 - esp["start"]) // 1024)],
                   capture=True).stdout
        clusters = re.search(r"provides (\d+) clusters", made)
        if not clusters or int(clusters.group(1)) < FAT32_MIN_CLUSTERS:
            raise MigrateError("the new EFI system partition is not a valid FAT32 filesystem")

    copies = [entry for entry in layout["partitions"] if entry["source"] is not None]
    with Guest(source, fmt=source_format, readonly=True, extra=[{"path": dest, "format": "raw", "readonly": False}]) as guest:
        g = guest.g
        # Two disks carry the same LVM identities once a physical volume is copied: keep LVM out of it.
        g.vg_activate_all(False)
        for entry in copies:
            src, dst = f"/dev/sda{entry['source']}", f"/dev/sdb{entry['num']}"
            size = g.blockdev_getsize64(src)
            if g.blockdev_getsize64(dst) != size:
                raise MigrateError(f"partition {entry['num']} was created with the wrong size")
            log(f"  copying partition {entry['num']} ({size // MIB} MiB)")
            g.copy_device_to_device(src, dst, sparse=True)
        g.sync()
        for entry in copies:
            src, dst = f"/dev/sda{entry['source']}", f"/dev/sdb{entry['num']}"
            if g.checksum_device("sha256", src) != g.checksum_device("sha256", dst):
                raise MigrateError(f"partition {entry['num']} differs from its source after the copy")
        g.shutdown()
    return layout
