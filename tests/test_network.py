import unittest

from pmig.network import (ifcfg_profile, interfaces_profiles, interfaces_without, netplan_profiles, netplan_without,
                          networkd_profiles, nm_profile, to_move)


def keyfile(kind, name, **extra):
    lines = ["[connection]", f"id={name}", f"type={kind}", f"interface-name={name}"]
    lines += [f"{key}={value}" for key, value in extra.items() if key in ("master", "controller")]
    if "parent" in extra:
        lines += ["[vlan]", f"parent={extra['parent']}"]
    return "\n".join(lines) + "\n"


class NetworkManager(unittest.TestCase):
    def moved(self, files):
        profiles = [nm_profile(path, text) for path, text in files.items()]
        return {key[0] for key in to_move(profiles)}

    def test_physical_profiles_move_and_virtual_ones_stay(self):
        files = {
            "eth0": keyfile("ethernet", "eth0"),
            "ib0": keyfile("infiniband", "ib0"),
            "dummy0": keyfile("dummy", "dummy0"),
            "wg0": keyfile("wireguard", "wg0"),
            "lo": keyfile("loopback", "lo"),
        }
        self.assertEqual(self.moved(files), {"eth0", "ib0"})

    def test_a_bond_follows_its_ports_and_a_vlan_its_parent(self):
        files = {
            "p1": keyfile("ethernet", "ens1", master="bond0"),
            "p2": keyfile("ethernet", "ens2", master="bond0"),
            "bond0": keyfile("bond", "bond0"),
            "vlan": keyfile("vlan", "bond0.100", parent="bond0", master="br100"),
            "br100": keyfile("bridge", "br100"),
            "br-internal": keyfile("bridge", "br-internal"),
            "tap": keyfile("tun", "tap0", master="br-internal"),
        }
        self.assertEqual(self.moved(files), {"p1", "p2", "bond0", "vlan", "br100"})


class Ifcfg(unittest.TestCase):
    DIR = "/etc/sysconfig/network-scripts"

    def test_static_ethernet_with_its_route_files(self):
        siblings = [f"{self.DIR}/ifcfg-eth0", f"{self.DIR}/route-eth0", f"{self.DIR}/rule-eth0", f"{self.DIR}/route-eth1"]
        profile = ifcfg_profile(f"{self.DIR}/ifcfg-eth0", 'DEVICE="eth0"\nBOOTPROTO=static\nIPADDR=10.0.0.5\nONBOOT=yes\n', siblings)
        self.assertTrue(profile.physical)
        self.assertEqual(profile.files, [f"{self.DIR}/ifcfg-eth0", f"{self.DIR}/route-eth0", f"{self.DIR}/rule-eth0"])

    def test_loopback_is_not_a_profile(self):
        self.assertIsNone(ifcfg_profile(f"{self.DIR}/ifcfg-lo", "DEVICE=lo\n", []))

    def test_bridge_over_a_physical_port_moves_and_a_dummy_stays(self):
        profiles = [
            ifcfg_profile(f"{self.DIR}/ifcfg-ens3", "DEVICE=ens3\nTYPE=Ethernet\nBRIDGE=br0\n", []),
            ifcfg_profile(f"{self.DIR}/ifcfg-br0", "DEVICE=br0\nTYPE=Bridge\nIPADDR=192.0.2.4\n", []),
            ifcfg_profile(f"{self.DIR}/ifcfg-ens3.20", "DEVICE=ens3.20\nVLAN=yes\n", []),
            ifcfg_profile(f"{self.DIR}/ifcfg-dummy0", "DEVICE=dummy0\nTYPE=dummy\n", []),
        ]
        moved = {key[0].rsplit("-", 1)[1] for key in to_move(profiles)}
        self.assertEqual(moved, {"ens3", "br0", "ens3.20"})


NETPLAN = """\
network:
  version: 2
  renderer: networkd
  ethernets:
    ens160:
      addresses: [10.1.2.3/24]
      routes:
        - to: default
          via: 10.1.2.1
    ens192: {}
  bonds:
    bond0:
      interfaces: [ens192]
  vlans:
    bond0.7:
      id: 7
      link: bond0
  bridges:
    br-app:
      interfaces: []
      addresses: [172.30.0.1/24]
  dummy-devices:
    dm0:
      addresses: [192.0.2.9/32]
"""


class Netplan(unittest.TestCase):
    def test_devices_on_the_source_nics_move(self):
        profiles = netplan_profiles("/etc/netplan/01.yaml", NETPLAN)
        moved = {(key[1], key[2]) for key in to_move(profiles)}
        self.assertEqual(moved, {("ethernets", "ens160"), ("ethernets", "ens192"), ("bonds", "bond0"), ("vlans", "bond0.7")})
        rest = netplan_without(NETPLAN, moved)
        self.assertIn("br-app", rest)
        self.assertIn("dm0", rest)
        self.assertNotIn("ens160", rest)
        self.assertNotIn("bonds", rest)

    def test_a_file_of_only_physical_nics_is_emptied(self):
        text = "network:\n  version: 2\n  ethernets:\n    eth0:\n      dhcp4: true\n"
        moved = {(key[1], key[2]) for key in to_move(netplan_profiles("f", text))}
        self.assertIsNone(netplan_without(text, moved))

    def test_garbage_has_no_profiles(self):
        self.assertEqual(netplan_profiles("f", "not: [valid"), [])
        self.assertEqual(netplan_profiles("f", "# empty\n"), [])


class Networkd(unittest.TestCase):
    def test_physical_match_and_stacked_devices(self):
        files = {
            "10-wan.network": "[Match]\nName=enp3s0\n[Network]\nAddress=10.0.0.2/24\nVLAN=vlan5\n",
            "vlan5.netdev": "[NetDev]\nName=vlan5\nKind=vlan\n[VLAN]\nId=5\n",
            "vlan5.network": "[Match]\nName=vlan5\n[Network]\nAddress=10.5.0.2/24\n",
            "dummy.netdev": "[NetDev]\nName=dummy0\nKind=dummy\n",
            "dummy.network": "[Match]\nName=dummy0\n[Network]\nAddress=192.0.2.1/32\n",
        }
        moved = {key[0] for key in to_move(networkd_profiles(files))}
        self.assertEqual(moved, {"10-wan.network", "vlan5.netdev", "vlan5.network"})


INTERFACES = """\
source /etc/network/interfaces.d/*

auto lo
iface lo inet loopback

auto eth0 dummy0
iface eth0 inet static
    address 10.0.0.9/24
    gateway 10.0.0.1

iface dummy0 inet manual
    pre-up ip link add dummy0 type dummy

allow-hotplug eth1
iface eth1 inet manual

auto br0
iface br0 inet dhcp
    bridge_ports eth1
"""


class Interfaces(unittest.TestCase):
    def test_stanzas_of_the_source_nics_are_removed(self):
        gone = {key[2] for key in to_move(interfaces_profiles("/etc/network/interfaces", INTERFACES))}
        self.assertEqual(gone, {"eth0", "eth1", "br0"})
        rest = interfaces_without(INTERFACES, gone)
        self.assertIn("source /etc/network/interfaces.d/*", rest)
        self.assertIn("iface lo inet loopback", rest)
        self.assertIn("auto dummy0", rest)
        self.assertIn("pre-up ip link add dummy0 type dummy", rest)
        for text in ("eth0", "eth1", "br0", "gateway"):
            self.assertNotIn(text, rest)


    def test_a_bridge_follows_its_port_from_another_file(self):
        files = {
            "/etc/network/interfaces": "auto lo\niface lo inet loopback\nsource /etc/network/interfaces.d/*\n",
            "/etc/network/interfaces.d/eth0": "auto eth0\niface eth0 inet manual\n",
            "/etc/network/interfaces.d/br0": "auto br0\niface br0 inet static\n    address 10.0.0.9/24\n    bridge_ports eth0\n",
        }
        stanzas = [profile for path, text in files.items() for profile in interfaces_profiles(path, text)]
        self.assertEqual({(key[0].rsplit("/", 1)[1], key[2]) for key in to_move(stanzas)}, {("eth0", "eth0"), ("br0", "br0")})


if __name__ == "__main__":
    unittest.main()
