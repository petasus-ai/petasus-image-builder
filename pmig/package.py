"""The containerDisk image of a verified disk, its push, and the request that registers it."""
from __future__ import annotations

import os
import shutil
import tempfile

from .convert import check_name
from .util import MigrateError, arch_info, locked, log, read_json, run, sha256_file, write_json

LOCAL_REPOSITORY = "localhost/petasus-migrate"
# The image catalog's distro types. A RHEL-family guest of any vendor takes the RHEL-family
# handling under `rocky`.
DISTRO_TYPES = {"el": "rocky", "deb": "ubuntu"}


def verified(out: str) -> dict:
    """The manifest of a disk whose verification passed for exactly these bytes."""
    disk = os.path.join(out, "disk.qcow2")
    for name in ("manifest.json", "verify.json"):
        if not os.path.isfile(os.path.join(out, name)) or not os.path.isfile(disk):
            raise MigrateError(f"{out} has no verified disk (run migrate, then verify)")
    manifest, verify = read_json(os.path.join(out, "manifest.json")), read_json(os.path.join(out, "verify.json"))
    digest = sha256_file(disk)
    if digest != manifest["disk"]["sha256"] or digest != verify["disk_sha256"]:
        raise MigrateError(f"{disk} changed after it was verified; run verify again")
    if not verify["passed"]:
        raise MigrateError(f"{disk} failed verification")
    return manifest


def local_ref(name: str, tag: str) -> str:
    return f"{LOCAL_REPOSITORY}/{name}:{tag}"


def package(args, root: str) -> str:
    check_name(args.name)
    out = os.path.join(args.output, args.name)
    with locked(out):
        manifest = verified(out)
        ref = local_ref(args.name, args.tag)
        context = tempfile.mkdtemp(prefix=".package.", dir=out)
        try:
            os.link(os.path.join(out, "disk.qcow2"), os.path.join(context, "disk.qcow2"))
            labels = {
                "ai.petasus.disk.virtual-size-bytes": manifest["disk"]["virtual_size"],
                "ai.petasus.disk.size-gib": manifest["disk"]["size_gib"],
                "ai.petasus.disk.sha256": manifest["disk"]["sha256"],
                "ai.petasus.guest.os": manifest["os"]["pretty"],
                "ai.petasus.guest.arch": manifest["os"]["arch"],
                "org.opencontainers.image.created": manifest["created"],
                "org.opencontainers.image.title": args.name,
            }
            cmd = ["podman", "build", "--quiet", "--platform", f"linux/{arch_info(manifest['os']['arch'])['platform']}",
                   "--file", os.path.join(root, "containerdisk", "Containerfile"), "--tag", ref]
            for key, value in labels.items():
                cmd += ["--label", f"{key}={value}"]
            log(f"building {ref}")
            run(cmd + [context], capture=True)
        finally:
            shutil.rmtree(context, ignore_errors=True)
    log(f"built {ref}")
    return ref


def push(args) -> dict:
    check_name(args.name)
    if not args.registry or not args.repository:
        raise MigrateError("REGISTRY and REPOSITORY are required")
    out = os.path.join(args.output, args.name)
    with locked(out):
        manifest = verified(out)
        ref = local_ref(args.name, args.tag)
        if run(["podman", "image", "exists", ref], check=False).returncode != 0:
            raise MigrateError(f"{ref} is not built (run package first)")
        label = run(["podman", "image", "inspect", "--format", '{{ index .Labels "ai.petasus.disk.sha256" }}', ref],
                    capture=True).stdout.strip()
        if label != manifest["disk"]["sha256"]:
            raise MigrateError(f"{ref} was built from another disk; run package again")
        target = f"{args.registry.rstrip('/')}/{args.repository.strip('/')}:{args.tag}"
        work = tempfile.mkdtemp(prefix=".push.", dir=out)
        try:
            digest_file = os.path.join(work, "digest")
            cmd = ["podman", "push", "--quiet", "--digestfile", digest_file]
            if args.authfile:
                cmd += ["--authfile", args.authfile]
            if args.ca:
                certs = os.path.join(work, "certs")
                os.makedirs(certs)
                shutil.copyfile(args.ca, os.path.join(certs, "ca.crt"))
                cmd += ["--cert-dir", certs]
            log(f"pushing {target}")
            run(cmd + [ref, f"docker://{target}"], capture=True)
            with open(digest_file, encoding="utf-8") as handle:
                digest = handle.read().strip()
        finally:
            shutil.rmtree(work, ignore_errors=True)

        source = args.source_ref or target
        guest = manifest["os"]
        request = {"image": {
            "name": args.name,
            "size": str(manifest["disk"]["size_gib"]),
            "source": f"docker://{source}",
            "distro_type": args.distro_type or DISTRO_TYPES[guest["family"]],
            "arch_type": arch_info(guest["arch"])["platform"],
            "description": f"{guest['pretty']}, migrated",
        }}
        write_json(os.path.join(out, "image.json"), request)
        record = {"pushed": target, "digest": digest, "source": request["image"]["source"]}
        write_json(os.path.join(out, "push.json"), record)
    log(f"pushed {target} ({digest}); the registration request is {os.path.join(out, 'image.json')}")
    return record
