"""Make a guest's cloud-init take its configuration from the platform's NoCloud disk."""
from __future__ import annotations

import yaml

CFG_DIR = "/etc/cloud/cloud.cfg.d"
DROPIN_SUFFIX = "petasus-platform.cfg"
DROPIN_NAME = f"zz-{DROPIN_SUFFIX}"
DROPIN = """\
# The platform delivers each VM's configuration on a NoCloud disk.
datasource_list: [ NoCloud, None ]
"""
# Modules the platform's user-data relies on, by the stage list that runs them.
REQUIRED_MODULES = {
    "cloud_init_modules": ("bootcmd", "write-files", "growpart", "resizefs", "set-hostname", "update-etc-hosts",
                           "users-groups", "ssh", "set-passwords"),
    "cloud_config_modules": ("runcmd",),
    "cloud_final_modules": ("scripts-user",),
}
# System configuration that keeps the platform's configuration from applying: `network` there
# outranks the datasource's network-config, and manual_cache_clean pins the first instance.
BLOCKING_KEYS = ("network", "manual_cache_clean")


def _load(text: str):
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def strip_blocking(text: str):
    """(new text or None, removed keys) for one cloud.cfg file."""
    data = _load(text)
    if data is None:
        return None, []
    removed = [key for key in BLOCKING_KEYS if key in data]
    if not removed:
        return None, []
    for key in removed:
        del data[key]
    body = yaml.safe_dump(data, default_flow_style=False, sort_keys=False) if data else ""
    return "#cloud-config\n" + body, removed


def dropin_name(existing: list) -> str:
    """A file name that sorts after every existing one: cloud-init merges in name order, last wins."""
    last = max((name for name in existing if not name.endswith(DROPIN_SUFFIX)), default="")
    if last < DROPIN_NAME:
        return DROPIN_NAME
    # Keep the leading '~'s of the last name and add one: '~' sorts after every other printable character.
    tildes = len(last) - len(last.lstrip("~"))
    return "~" * (tildes + 1) + DROPIN_SUFFIX


def _module_names(entries) -> set:
    names = set()
    for entry in entries or []:
        name = entry[0] if isinstance(entry, list) and entry else entry
        if isinstance(name, str):
            names.add(name.replace("_", "-"))
    return names


def missing_modules(merged: dict) -> list:
    """Required modules that no stage runs. Releases differ in which stage lists a module."""
    stages = [stage for stage in REQUIRED_MODULES if stage in merged]
    if not stages:
        return []
    present = set().union(*(_module_names(merged[stage]) for stage in stages))
    return [module for modules in REQUIRED_MODULES.values() for module in modules if module not in present]


def apply(guest) -> None:
    """Edit the configuration of a mounted guest that has cloud-init installed."""
    paths = ["/etc/cloud/cloud.cfg"] + guest.glob(f"{CFG_DIR}/*.cfg")
    merged = {}
    for path in paths:
        text = guest.read(path)
        if text is None:
            continue
        new, removed = strip_blocking(text)
        if new is not None:
            guest.backup_copy(path)
            if new.strip() == "#cloud-config":
                guest.g.rm(path)
            else:
                guest.write(path, new)
            guest.note("INFO", "cloud-init", f"{path}: removed {', '.join(removed)} (the original is in the backup directory)")
        merged.update(_load(new if new is not None else text) or {})
    for module in missing_modules(merged):
        guest.note("WARN", "cloud-init-modules", f"the cloud-init configuration does not run {module}")
    names = [path.rsplit("/", 1)[1] for path in guest.glob(f"{CFG_DIR}/*.cfg")]
    guest.write(f"{CFG_DIR}/{dropin_name(names)}", DROPIN, mode=0o644)
    for path in ("/etc/cloud/cloud-init.disabled", "/etc/cloud/ds-identify.cfg"):
        if guest.exists(path):
            guest.backup_move(path)
            guest.note("INFO", "cloud-init", f"moved {path} aside")
    # State of the source instance, and seeds that would be read before the platform's disk.
    for path in guest.glob("/var/lib/cloud/*"):
        guest.g.rm_rf(path)
