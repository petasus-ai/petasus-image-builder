"""Command line: inspect, migrate, verify, package, push."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .util import MigrateError, Refused, log
from .util import setting as env

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="petasus-migrate", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, source=False):
        if source:
            p.add_argument("--src", default=env("SRC"), help="source disk image (qcow2 or raw)")
            p.add_argument("--src-format", default=env("SRC_FORMAT") or None, help="source format; detected when unset")
        p.add_argument("--name", default=env("NAME"), help="image name; the output goes to OUTPUT/NAME")
        p.add_argument("--output", default=env("OUTPUT", os.path.join(ROOT, "output")))

    def policy(p):
        p.add_argument("--uefi-convert", default=env("UEFI_CONVERT", "auto"), choices=("auto", "never"))
        p.add_argument("--missing-mounts", default=env("MISSING_MOUNTS", "refuse"), choices=("refuse", "nofail"))

    p = sub.add_parser("inspect", help="report what a source disk holds and whether it converts")
    common(p, source=True)
    policy(p)

    p = sub.add_parser("migrate", help="convert a source disk and verify that the result boots")
    common(p, source=True)
    p.add_argument("--generalize", default=env("GENERALIZE", "preserve"), choices=("preserve", "template"))
    p.add_argument("--network", default=env("NETWORK", "reset"), choices=("reset", "keep"))
    policy(p)
    p.add_argument("--build-repo-file", default=env("BUILD_REPO_FILE") or None)
    p.add_argument("--el-repo-iso", default=env("EL_REPO_ISO") or None)
    p.add_argument("--skip-verify", action="store_true", default=env("SKIP_VERIFY") == "1")

    p = sub.add_parser("verify", help="boot the converted disk the way the platform does and check it")
    common(p)

    p = sub.add_parser("package", help="build the containerDisk image of a verified disk")
    common(p)
    p.add_argument("--tag", default=env("TAG", "latest"))

    p = sub.add_parser("push", help="push the containerDisk image and write the registration request")
    common(p)
    p.add_argument("--registry", default=env("REGISTRY"))
    p.add_argument("--repository", default=env("REPOSITORY"))
    p.add_argument("--tag", default=env("TAG", "latest"))
    p.add_argument("--authfile", default=env("REGISTRY_AUTHFILE") or None)
    p.add_argument("--ca", default=env("REGISTRY_CA") or None)
    p.add_argument("--source-ref", default=env("SOURCE_REF") or None)
    p.add_argument("--distro-type", default=env("DISTRO_TYPE") or None)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            from .inspect import inspect_disk

            if not args.src:
                raise MigrateError("SRC is required")
            info = inspect_disk(args.src, args.src_format, args.missing_mounts, args.uefi_convert)
            json.dump(info, sys.stdout, indent=2, sort_keys=True)
            print()
            for reason in info["refusals"]:
                log(f"REFUSED: {reason}")
            return 3 if info["refusals"] else 0
        if not args.name:
            raise MigrateError("NAME is required")
        if args.command == "migrate":
            from .convert import migrate

            if not args.src:
                raise MigrateError("SRC is required")
            migrate(args, ROOT)
        elif args.command == "verify":
            from .verify import verify_output

            verify_output(os.path.join(args.output, args.name))
        elif args.command == "package":
            from .package import package

            package(args, ROOT)
        elif args.command == "push":
            from .package import push

            push(args)
    except Refused as err:
        for reason in err.reasons:
            log(f"REFUSED: {reason}")
        return 3
    except MigrateError as err:
        log(f"FAILED: {err}")
        return 1
    except RuntimeError as err:  # libguestfs reports its failures this way
        log(f"FAILED: {err}")
        return 1
    return 0
