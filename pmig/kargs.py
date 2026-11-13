"""Kernel arguments the platform needs, in every place a distribution keeps a command line."""
from __future__ import annotations

import re

from .refs import GRUBENV_FILES

# net.ifnames / biosdevname force eth0-style names; the others turn cloud-init or its NoCloud disk off.
REMOVE_KEYS = ("net.ifnames", "biosdevname", "network-config", "ds", "ci.ds", "ci.datasource")
GPU_BLACKLIST = ("nouveau", "amdgpu")

# VAR="value", VAR='value' or VAR=word, then an optional comment.
_ASSIGN = re.compile(r"""^(\s*)(GRUB_CMDLINE_LINUX(?:_DEFAULT)?)=(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|([^\s"'#]*))(\s*(?:#.*)?)$""")


def edit_args(args: str, console: str, ensure: bool = True) -> str:
    """Drop the arguments the platform cannot run with and, with ensure, add the ones it needs."""
    kept, blacklist, has_console, has_serial = [], [], False, False
    for token in args.split():
        key, _, value = token.partition("=")
        if key in REMOVE_KEYS or (key == "cloud-init" and value == "disabled"):
            continue
        if key == "modprobe.blacklist":
            blacklist += [module for module in value.split(",") if module]
            if ensure:
                continue
        if key == "nouveau.modeset" and ensure:
            continue
        if key == "console":
            has_console = True
            has_serial = has_serial or value.split(",")[0] == console
        kept.append(token)
    if ensure:
        for module in GPU_BLACKLIST:
            if module not in blacklist:
                blacklist.append(module)
        if not has_console:
            kept.append("console=tty0")
        if not has_serial:
            kept.append(f"console={console},115200n8")
        kept += ["modprobe.blacklist=" + ",".join(blacklist), "nouveau.modeset=0"]
    return " ".join(kept)


def edit_grub_defaults(files: dict, console: str) -> dict:
    """files: {path: text} in the order GRUB reads them. Returns the changed ones.

    Removal applies to every literal assignment. The additions go to the assignment that decides
    GRUB_CMDLINE_LINUX; they are skipped when either variable already carries them. A value computed
    by the shell stays as it is: the additions follow it as `VAR="$VAR ..."`.
    """
    lines_of = {path: text.splitlines() for path, text in files.items()}
    final = {}  # variable -> its last assignment
    for path, lines in lines_of.items():
        for index, line in enumerate(lines):
            match = _ASSIGN.match(line)
            if not match:
                continue
            indent, variable, double, single, bare, tail = match.groups()
            value = next(part for part in (double, single, bare) if part is not None)
            entry = {"path": path, "index": index, "indent": indent, "quote": "'" if single is not None else '"',
                     "tail": tail, "dynamic": "$(" in value or "`" in value, "value": value}
            if not entry["dynamic"]:
                entry["value"] = edit_args(value, console, ensure=False)
                lines[index] = _render(variable, entry, entry["value"].split())
            final[variable] = entry
    have = " ".join(entry["value"] for entry in final.values() if not entry["dynamic"]).split()
    target = edit_args(" ".join(have), console).split()
    blacklist = next(token for token in target if token.startswith("modprobe.blacklist="))
    gpu_done = blacklist in have and "nouveau.modeset=0" in have
    additions = [token for token in target if token.startswith("console=") and token not in have]
    additions += [] if gpu_done else [blacklist, "nouveau.modeset=0"]
    stale = () if gpu_done else ("modprobe.blacklist", "nouveau.modeset")
    appended = []
    for variable, extra in (("GRUB_CMDLINE_LINUX_DEFAULT", []), ("GRUB_CMDLINE_LINUX", additions)):
        entry = final.get(variable)
        if entry is None:
            continue
        if entry["dynamic"]:
            if extra:
                appended.append((entry["path"], entry["index"] + 1, f'{variable}="${variable} {" ".join(extra)}"'))
            continue
        tokens = [token for token in entry["value"].split() if token.partition("=")[0] not in stale]
        lines_of[entry["path"]][entry["index"]] = _render(variable, entry, tokens + extra)
    if "GRUB_CMDLINE_LINUX" not in final and lines_of:
        path = next(iter(lines_of))
        appended.append((path, len(lines_of[path]), f'GRUB_CMDLINE_LINUX="{" ".join(additions)}"'))
    for path, index, line in sorted(appended, reverse=True):
        lines_of[path].insert(index, line)
    changed = {}
    for path, lines in lines_of.items():
        text = "\n".join(lines) + "\n"
        if text != files[path]:
            changed[path] = text
    return changed


def _render(variable: str, entry: dict, tokens: list) -> str:
    return f"{entry['indent']}{variable}={entry['quote']}{' '.join(tokens)}{entry['quote']}{entry['tail']}"


def edit_bls_entry(text: str, console: str) -> str:
    """A Boot Loader Specification entry. An options line that expands $kernelopts gets no additions:
    the environment block's kernelopts carries them."""
    lines = []
    for line in text.splitlines():
        match = re.match(r"^(options\s+)(.*)$", line)
        if match:
            line = match.group(1) + edit_args(match.group(2), console, ensure="$kernelopts" not in match.group(2))
        lines.append(line)
    return "\n".join(lines) + "\n"


def edit_kernelopts(text: str, console: str) -> str:
    """The kernelopts variable of a GRUB environment block."""
    return re.sub(r"^(kernelopts=)(.*)$", lambda m: m.group(1) + edit_args(m.group(2), console), text, flags=re.M)


def apply(guest, console: str) -> None:
    paths = ["/etc/default/grub"] + guest.glob("/etc/default/grub.d/*.cfg")
    files = {path: guest.read(path) for path in paths if guest.is_file(path)}
    if "/etc/default/grub" not in files:
        files = {"/etc/default/grub": "", **files}
    for path, text in edit_grub_defaults(files, console).items():
        guest.write(path, text)
        guest.note("INFO", "kernel-args", f"{path} updated")
    for path in guest.glob("/boot/loader/entries/*.conf"):
        if guest.edit(path, lambda text: edit_bls_entry(text, console)):
            guest.note("INFO", "kernel-args", f"{path} updated")
    if guest.is_file("/etc/kernel/cmdline"):
        guest.edit("/etc/kernel/cmdline", lambda text: edit_args(text, console) + "\n")
    for path in guest.paths(GRUBENV_FILES):
        if guest.edit_grubenv(path, lambda text: edit_kernelopts(text, console)):
            guest.note("INFO", "kernel-args", f"{path} kernelopts updated")
