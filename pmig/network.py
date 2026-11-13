"""Set aside the network configuration that belongs to the source machine's NICs.

The platform configures every NIC it attaches through cloud-init. Profiles of the source's physical
NICs, and the bonds, bridges and VLANs built on them, describe hardware and addresses that do not
exist here; they move to the backup directory. Everything else (loopback, dummy devices, bridges
without a physical port, tunnels) stays.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from .platform import POD_NIC_LINK

NM_PHYSICAL = ("ethernet", "802-3-ethernet", "wifi", "802-11-wireless", "infiniband", "gsm", "cdma")
NETPLAN_PHYSICAL = ("ethernets", "wifis", "modems")
NETPLAN_SECTIONS = NETPLAN_PHYSICAL + ("bonds", "bridges", "vlans", "tunnels", "vrfs", "dummy-devices", "nm-devices")
_PHYSICAL_NAME = re.compile(r"^(eth|en|em|ib|wl|ww|p\d+p\d+|sl)\w*$")
LEASES = (
    "/var/lib/dhclient/*", "/var/lib/dhcp/*.lease*", "/var/lib/NetworkManager/*.lease",
    "/var/lib/NetworkManager/*.lease6", "/var/lib/systemd/network/*", "/var/lib/dhcpcd/*.lease",
)
NAMING_RULES = ("/etc/udev/rules.d/70-persistent-net.rules", "/etc/udev/rules.d/70-persistent-ipoib.rules",
                "/etc/udev/rules.d/80-net-setup-link.rules", "/etc/systemd/network/99-default.link")


@dataclass
class Profile:
    key: tuple                       # (path,) or (path, section, device)
    ids: set = field(default_factory=set)      # names other profiles may refer to
    physical: bool = False
    masters: set = field(default_factory=set)  # controllers this profile is a port of
    parents: set = field(default_factory=set)  # devices this profile is stacked on
    ports: set = field(default_factory=set)    # ports this profile controls
    files: list = field(default_factory=list)  # files that go with it


def to_move(profiles: list) -> set:
    """Keys of the physical profiles and of everything stacked on or enslaving them."""
    moved = {profile.key for profile in profiles if profile.physical}
    changed = True
    while changed:
        changed = False
        moved_profiles = [profile for profile in profiles if profile.key in moved]
        moved_ids = set().union(*(profile.ids for profile in moved_profiles)) if moved_profiles else set()
        moved_masters = set().union(*(profile.masters for profile in moved_profiles)) if moved_profiles else set()
        for profile in profiles:
            if profile.key in moved:
                continue
            if (profile.masters | profile.parents | profile.ports) & moved_ids or profile.ids & moved_masters:
                moved.add(profile.key)
                changed = True
    return moved


def _ini(text: str) -> dict:
    """{section: [(key, value)]} of an INI-style file; keys may repeat."""
    sections, current = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], [])
        elif "=" in line and current is not None:
            key, value = line.split("=", 1)
            current.append((key.strip(), value.strip()))
    return sections


def _get(sections: dict, section: str, key: str) -> list:
    return [value for name, value in sections.get(section, []) if name == key and value]


def nm_profile(path: str, text: str) -> Profile:
    ini = _ini(text)
    kind = (_get(ini, "connection", "type") or [""])[0]
    profile = Profile(key=(path,), files=[path], physical=kind in NM_PHYSICAL)
    for key in ("id", "uuid", "interface-name"):
        profile.ids.update(_get(ini, "connection", key))
    for key in ("master", "controller"):
        profile.masters.update(_get(ini, "connection", key))
    for section in ("vlan", "macvlan", "infiniband"):
        profile.parents.update(_get(ini, section, "parent"))
    return profile


def _shell_vars(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match:
            values[match.group(1).upper()] = match.group(2).strip().strip('"').strip("'")
    return values


def ifcfg_profile(path: str, text: str, siblings: list):
    """A profile of /etc/sysconfig/network-scripts, or None for loopback."""
    values = _shell_vars(text)
    device = values.get("DEVICE") or path.rsplit("ifcfg-", 1)[1]
    if device == "lo" or values.get("TYPE", "").lower() == "loopback":
        return None
    kind = values.get("TYPE", "").lower()
    vlan = values.get("VLAN", "").lower() == "yes"
    stacked = vlan or kind in ("bond", "bridge", "team", "vlan") or values.get("BONDING_MASTER", "").lower() == "yes"
    virtual = kind in ("dummy", "tunnel", "ipip", "gre", "sit", "vxlan", "wireguard", "tap", "tun", "veth")
    profile = Profile(key=(path,), files=[path])
    profile.ids.update(value for value in (device, values.get("NAME"), values.get("UUID")) if value)
    profile.masters.update(value for key in ("MASTER", "BRIDGE", "TEAM_MASTER") if (value := values.get(key)))
    if vlan or kind == "vlan":
        parent = values.get("PHYSDEV") or (device.split(".")[0] if "." in device else "")
        if parent:
            profile.parents.add(parent)
    profile.physical = not stacked and not virtual and (
        kind in ("ethernet", "infiniband", "wireless") or bool(_PHYSICAL_NAME.match(device)) or kind == "")
    base = device.split(":")[0]
    suffix = path.rsplit("ifcfg-", 1)[1]
    profile.files += [other for other in siblings
                      if re.search(rf"/(route6?|rule6?)-({re.escape(base)}|{re.escape(suffix)})$", other)]
    return profile


def netplan_profiles(path: str, text: str) -> list:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    network = data.get("network") if isinstance(data, dict) else None
    if not isinstance(network, dict):
        return []
    profiles = []
    for section in NETPLAN_SECTIONS:
        devices = network.get(section)
        if not isinstance(devices, dict):
            continue
        for name, config in devices.items():
            config = config if isinstance(config, dict) else {}
            profile = Profile(key=(path, section, name), ids={name}, physical=section in NETPLAN_PHYSICAL)
            if config.get("set-name"):
                profile.ids.add(config["set-name"])
            profile.ports.update(config.get("interfaces") or [])
            if config.get("link"):
                profile.parents.add(config["link"])
            profiles.append(profile)
    return profiles


def netplan_without(text: str, devices: set):
    """The file without those (section, device) entries; None when no device is left."""
    data = yaml.safe_load(text)
    network = data["network"]
    for section, name in devices:
        network.get(section, {}).pop(name, None)
        if not network.get(section):
            network.pop(section, None)
    if not any(section in network for section in NETPLAN_SECTIONS):
        return None
    return yaml.safe_dump(data, default_flow_style=False, sort_keys=False)


def networkd_profiles(files: dict) -> list:
    """Profiles of /etc/systemd/network: .netdev files define devices, .network files configure them."""
    netdevs = {}
    profiles = []
    for path, text in files.items():
        if path.endswith(".netdev"):
            ini = _ini(text)
            for name in _get(ini, "NetDev", "Name"):
                netdevs[name] = Profile(key=(path,), ids={name}, files=[path])
    for path, text in files.items():
        if not path.endswith(".network"):
            continue
        ini = _ini(text)
        names = [name for value in _get(ini, "Match", "Name") for name in value.split()]
        profile = Profile(key=(path,), ids=set(names), files=[path])
        profile.masters.update(_get(ini, "Network", "Bond") + _get(ini, "Network", "Bridge"))
        stacked = [name for key in ("VLAN", "MACVLAN", "VXLAN") for value in _get(ini, "Network", key) for name in value.split()]
        for child in stacked:
            if child in netdevs:
                netdevs[child].parents.update(names)
        defined = any(name in netdevs for name in names)
        profile.physical = bool(names) and not defined and all(name != "lo" for name in names) and any(
            _PHYSICAL_NAME.match(name.rstrip("*")) or "*" in name for name in names)
        if not names and (_get(ini, "Match", "MACAddress") or _get(ini, "Match", "Path") or _get(ini, "Match", "Driver")):
            profile.physical = True
        if defined:
            # The .network of a defined device follows that device.
            profile.parents.update(names)
        profiles.append(profile)
    return profiles + list(netdevs.values())


_STANZA = re.compile(r"^\s*(iface|auto|allow-\S+|mapping|source|source-directory|no-auto-down|no-scripts)\b")


def interfaces_profiles(path: str, text: str) -> list:
    """Profiles of an ifupdown interfaces file, one per iface stanza."""
    profiles = {}
    current = None
    for line in text.splitlines():
        words = line.split()
        if _STANZA.match(line):
            current = None
            if words[0] == "iface" and len(words) > 1 and words[1] != "lo":
                name = words[1]
                current = profiles.setdefault(name, Profile(key=(path, "iface", name), ids={name}))
                if "." in name:
                    current.parents.add(name.split(".")[0])
        elif current is not None and words:
            option = words[0].replace("_", "-")
            if option in ("bridge-ports", "bond-slaves", "slaves"):
                current.ports.update(word for word in words[1:] if word != "none")
            elif option == "bond-master":
                current.masters.update(words[1:])
            elif option == "vlan-raw-device":
                current.parents.update(words[1:])
    for name, profile in profiles.items():
        profile.physical = bool(_PHYSICAL_NAME.match(name)) and not (profile.ports or profile.parents)
    return list(profiles.values())


def interfaces_without(text: str, names: set) -> str:
    """The interfaces file without the stanzas of those interfaces."""
    lines, skipping = [], False
    for line in text.splitlines():
        words = line.split()
        if _STANZA.match(line):
            skipping = False
            if words[0] == "iface" and len(words) > 1 and words[1] in names:
                skipping = True
                continue
            if words[0] == "auto" or words[0].startswith("allow-"):
                kept = [word for word in words[1:] if word not in names]
                if not kept:
                    continue
                line = " ".join([words[0]] + kept)
        elif skipping:
            continue
        lines.append(line)
    return "\n".join(lines) + "\n"


def reset(guest) -> None:
    """Move the source's NIC profiles of a mounted guest to the backup directory."""
    moved_files, kept = [], []

    def move(path):
        if guest.exists(path):
            guest.backup_move(path)
            moved_files.append(path)

    # NetworkManager keyfiles and ifcfg files: one profile per file.
    profiles = []
    for path in guest.glob("/etc/NetworkManager/system-connections/*"):
        if guest.is_file(path):
            profiles.append(nm_profile(path, guest.read(path) or ""))
    scripts = guest.glob("/etc/sysconfig/network-scripts/*")
    for path in scripts:
        if re.search(r"/ifcfg-[^/]+$", path) and guest.is_file(path) and not re.search(r"\.(bak|orig|rpmsave|rpmnew)$|~$", path):
            profile = ifcfg_profile(path, guest.read(path) or "", scripts)
            if profile:
                profiles.append(profile)
    networkd = {path: guest.read(path) or "" for path in guest.glob("/etc/systemd/network/*.net*") if guest.is_file(path)}
    profiles += networkd_profiles(networkd)
    moving = to_move(profiles)
    for profile in profiles:
        if profile.key in moving:
            for path in profile.files:
                move(path)
        else:
            kept += profile.files

    # netplan and ifupdown: several devices per file.
    netplan = {path: guest.read(path) or "" for path in guest.glob("/etc/netplan/*.yaml")}
    devices = [profile for path, text in netplan.items() for profile in netplan_profiles(path, text)]
    moving = to_move(devices)
    for path, text in netplan.items():
        gone = {(key[1], key[2]) for key in moving if key[0] == path}
        mine = [profile for profile in devices if profile.key[0] == path]
        if not gone and mine:
            kept.append(path)
            continue
        rest = netplan_without(text, gone) if mine else None
        guest.backup_copy(path)
        moved_files.append(path)
        if rest is None:
            guest.g.rm(path)
        else:
            guest.write(path, rest)
            kept.append(path)
    interfaces = {}
    for path in ["/etc/network/interfaces"] + guest.glob("/etc/network/interfaces.d/*"):
        text = guest.read(path)
        if text is not None:
            interfaces[path] = text
    stanzas = [profile for path, text in interfaces.items() for profile in interfaces_profiles(path, text)]
    moving = to_move(stanzas)
    for path, text in interfaces.items():
        gone = {key[2] for key in moving if key[0] == path}
        if gone:
            guest.backup_copy(path)
            guest.write(path, interfaces_without(text, gone))
            moved_files.append(f"{path} ({', '.join(sorted(gone))})")

    # Rules that pin NIC names to the source's MAC addresses, or turn predictable names off.
    for path in NAMING_RULES:
        if guest.exists(path) or guest.g.is_symlink(path):
            guest.backup_move(path) if not guest.g.is_symlink(path) else guest.g.rm(path)
            moved_files.append(path)
    for path in guest.glob("/etc/systemd/network/*.link"):
        if path != POD_NIC_LINK:
            move(path)
    if moved_files:
        guest.note("INFO", "network", "moved to the backup directory: " + ", ".join(moved_files))
    if kept:
        guest.note("INFO", "network", "left in place: " + ", ".join(sorted(set(kept))))


def clear_leases(guest) -> None:
    for pattern in LEASES:
        for path in guest.glob(pattern):
            guest.g.rm_rf(path)
