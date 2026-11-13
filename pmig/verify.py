"""Boot the converted disk the way the platform does and check what the platform relies on.

The disk itself is never booted: every start runs on a throwaway overlay with a fresh firmware
variable store, on the device model the platform's virtual machines have (UEFI without Secure Boot,
non-transitional virtio disks, the pod NIC at PCI 0000:01:00.0, a NoCloud disk shaped like the
platform's).
"""
from __future__ import annotations

import base64
import glob
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
import uuid

from .platform import FORBIDDEN_RPCS, POD_NIC, REQUIRED_RPCS
from .qga import Agent, AgentError
from .util import (MigrateError, arch_info, first_existing, locked, log, qemu_img_info, read_json, run, setting,
                   sha256_file, write_json)

GROW_BYTES = 2 << 30
VERIFY_USER = "pmigverify"
PASSWORD_USER = "pmigpw"
SCRATCH_TOOLS = ("lvm", "mkfs.xfs", "wipefs", "blkdiscard", "blockdev", "dmsetup")

USER_DATA = """\
#cloud-config

hostname: "{hostname}"
fqdn: "{hostname}"
manage_etc_hosts: localhost

updates:
  network:
    when: ['boot-new-instance', 'boot']

bootcmd:
  - [cloud-init-per, instance, edgetron-machine-id, sh, -c,
     "rm -f /etc/machine-id /var/lib/dbus/machine-id && systemd-machine-id-setup && {{ ln -sf /etc/machine-id /var/lib/dbus/machine-id || true; }}"]
  - [cloud-init-per, instance, edgetron-hosts-prune, sh, -c,
     "sed -i '/^127\\\\.0\\\\.1\\\\.1[[:space:]]/d' /etc/hosts || true"]

ssh_pwauth: true

users:
  - default
  - name: "{user}"
    gecos: "{user}"
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys:
      - "{key}"
  - name: "{password_user}"
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash

chpasswd:
  expire: false
  users:
  - name: "{password_user}"
    password: "{password}"
    type: text

ssh_deletekeys: True
ssh_genkeytypes: ['rsa', 'ecdsa', 'ed25519']

ssh_authorized_keys:
  - "{key}"

write_files:
- content: {script}
  encoding: b64
  path: /tmp/user_bash_script.sh
  permissions: '0755'

runcmd:
  - '/tmp/user_bash_script.sh'
"""
USER_SCRIPT = "#!/bin/bash\nmkdir -p /var/lib/pmig-verify && date >/var/lib/pmig-verify/runcmd-marker\n"
NETWORK_CONFIG = f"version: 2\nethernets:\n  {POD_NIC}:\n    dhcp4: true\n"

# Runs as root in the guest; prints key=value lines.
PROBE = r"""
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C
kv() { printf '%s=%s\n' "$1" "$(printf '%s' "$2" | tr '\n' ' ')"; }
kv efi "$([ -d /sys/firmware/efi ] && echo yes || echo no)"
root=$(findmnt -no SOURCE / | sed 's/\[.*//')
kv root_source "$root"
kv root_disks "$(lsblk -sno NAME,TYPE "$root" 2>/dev/null | awk '$2=="disk"{gsub(/[^a-z0-9]/,"",$1); print $1}' | sort -u)"
kv root_fstype "$(findmnt -no FSTYPE /)"
kv root_fs_bytes "$(df -B1 --output=size / | tail -n 1 | tr -d ' ')"
kv root_dev_bytes "$(lsblk -bdno SIZE "$root" 2>/dev/null | head -n 1 | tr -d ' ')"
disk=vda
end=0
for part in /sys/block/$disk/$disk*; do
    [ -r "$part/start" ] || continue
    last=$(( $(cat "$part/start") + $(cat "$part/size") ))
    [ "$last" -gt "$end" ] && end=$last
done
kv disk_tail_sectors "$(( $(cat /sys/block/$disk/size 2>/dev/null || echo 0) - end ))"
if command -v cloud-init >/dev/null; then
    kv ci_status "$(cloud-init status 2>&1 | head -n 1)"
    kv ci_version "$(cloud-init --version 2>&1)"
fi
kv ci_datasource "$(cat /run/cloud-init/result.json 2>/dev/null | tr -d '\n' | sed 's/.*"datasource": *"\([^"]*\)".*/\1/')"
kv ci_errors "$(python3 -c 'import json;print(len(json.load(open("/run/cloud-init/result.json"))["v1"]["errors"]))' 2>/dev/null)"
kv hostname "$(hostname)"
kv marker "$([ -s /var/lib/pmig-verify/runcmd-marker ] && echo yes || echo no)"
kv default_routes "$(ip -4 route show default | sed 's/ *$//' | tr '\n' ';')"
kv nic_pci "$(basename "$(readlink -f /sys/class/net/__NIC__/device/..)" 2>/dev/null)"
props=$(udevadm info -q property -p /sys/class/net/__NIC__ 2>/dev/null)
kv nic_name_path "$(printf '%s\n' "$props" | sed -n 's/^ID_NET_NAME_PATH=//p')"
kv nic_name_slot "$(printf '%s\n' "$props" | sed -n 's/^ID_NET_NAME_SLOT=//p')"
kv nics "$(ls /sys/class/net | tr '\n' ' ')"
kv selinux "$(getenforce 2>/dev/null || echo Disabled)"
kv machine_id "$(cat /etc/machine-id 2>/dev/null)"
kv scratch_missing "$(for t in __TOOLS__; do command -v "$t" >/dev/null || printf '%s ' "$t"; done)"
kv lvm_autoactivation "$(lvm vgcreate --help 2>&1 | grep -c -- '--setautoactivation')"
kv lvm_devicesfile "$(lvm vgcreate --help 2>&1 | grep -c -- '--devicesfile')"
kv failed_units "$(systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}')"
kv qga_bool "$(getsebool virt_qemu_ga_read_nonsecurity_files 2>/dev/null)"
kv password_hash "$(getent shadow __PWUSER__ | cut -d: -f2 | cut -c1-3)"
kv kernel "$(uname -r)"
kv boot_id "$(cat /proc/sys/kernel/random/boot_id)"
kv os "$(. /etc/os-release && echo "$PRETTY_NAME")"
"""


class Checks:
    def __init__(self):
        self.items = []

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.items.append({"name": name, "status": status, "detail": detail})
        log(f"  {status.upper():10s} {name}" + (f": {detail}" if detail else ""))

    def expect(self, name: str, ok: bool, detail: str = "", soft: bool = False) -> bool:
        self.add(name, "pass" if ok else ("restricted" if soft else "fail"), detail)
        return ok

    @property
    def failed(self) -> list:
        return [item for item in self.items if item["status"] == "fail"]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Machine:
    """One start of an overlay of the disk."""

    def __init__(self, disk: str, workdir: str, arch: str, index: int, key_pub: str, password: str):
        self.workdir = os.path.join(workdir, f"boot{index}")
        os.makedirs(self.workdir)
        self.info = arch_info(arch)
        self.arch = arch
        self.index = index
        self.hostname = f"pmig-verify-{index}"
        self.mac = "02:50:4d:49:47:%02x" % index
        self.ssh_port, self.metrics_port = free_port(), free_port()
        self.serial_log = os.path.join(self.workdir, "serial.log")
        # UNIX socket paths are limited to 107 bytes: the sockets live in a short directory of their own.
        self.sockets = tempfile.mkdtemp(prefix="pmig-", dir="/tmp")
        self.agent = Agent(os.path.join(self.sockets, "qga"))
        self.process = None

        virtual = qemu_img_info(disk, "qcow2")["virtual-size"]
        self.overlay = os.path.join(self.workdir, "overlay.qcow2")
        run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", os.path.abspath(disk), self.overlay,
             str(virtual + GROW_BYTES)])
        self.vars = os.path.join(self.workdir, "VARS.fd")
        shutil.copyfile(first_existing(self.info["ovmf_vars"], "UEFI variable store template"), self.vars)
        self.seed = self._seed(key_pub, password)

    def _seed(self, key_pub: str, password: str) -> str:
        files = {
            "meta-data": json.dumps({"instance-id": str(uuid.uuid4()), "local-hostname": self.hostname}) + "\n",
            "user-data": USER_DATA.format(hostname=self.hostname, user=VERIFY_USER, key=key_pub,
                                          password_user=PASSWORD_USER, password=password,
                                          script=base64.b64encode(USER_SCRIPT.encode()).decode()),
            "network-config": NETWORK_CONFIG,
        }
        paths = []
        for name, content in files.items():
            path = os.path.join(self.workdir, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
            paths.append(path)
        seed = os.path.join(self.workdir, "seed.iso")
        tool = first_existing(["xorriso", "genisoimage", "mkisofs"], "an ISO 9660 writer (xorriso)")
        cmd = [tool, "-as", "mkisofs"] if os.path.basename(tool) == "xorriso" else [tool]
        run(cmd + ["-quiet", "-o", seed, "-V", "cidata", "-J", "-r"] + paths, capture=True)
        return seed

    def command(self) -> list:
        """The QEMU command line, after the one libvirt builds for the platform's virtual machines."""
        info, work = self.info, self.workdir
        qemu = first_existing(info["qemu"], "QEMU")
        code = first_existing(info["ovmf_code"], "UEFI firmware")

        def device(**properties) -> list:
            return ["-device", json.dumps(properties)]

        def blockdev(node: str, filename: str, driver: str, readonly: bool) -> list:
            return ["-blockdev", json.dumps({"driver": "file", "filename": filename, "node-name": f"{node}-file",
                                             "read-only": readonly}),
                    "-blockdev", json.dumps({"driver": driver, "file": f"{node}-file", "node-name": node,
                                             "read-only": readonly})]

        cmd = [qemu, "-name", f"pmig-verify-{self.index}", "-no-user-config", "-nodefaults", "-display", "none",
               "-cpu", "host", "-smp", "2", "-m", "2048", "-boot", "strict=on",
               "-smbios", f"type=1,manufacturer=KubeVirt,product=None,uuid={uuid.uuid4()},family=KubeVirt"]
        cmd += blockdev("pflash0", code, "raw", True) + blockdev("pflash1", self.vars, "raw", False)
        if self.arch == "x86_64":
            cmd += ["-machine", "q35,accel=kvm,usb=off,vmport=off,pflash0=pflash0,pflash1=pflash1"]
        else:
            cmd += ["-machine", "virt,accel=kvm,gic-version=host,pflash0=pflash0,pflash1=pflash1"]
        for number in range(1, 17):
            slot, function = 2 + (number - 1) // 8, (number - 1) % 8
            properties = {"driver": "pcie-root-port", "port": 15 + number, "chassis": number, "id": f"pci.{number}",
                          "bus": "pcie.0", "addr": f"0x{slot:x}.0x{function:x}"}
            if function == 0:
                properties["multifunction"] = True
            cmd += device(**properties)
        cmd += device(driver="virtio-serial-pci-non-transitional", id="virtio-serial0", bus="pci.6", addr="0x0")
        cmd += blockdev("root", self.overlay, "qcow2", False)
        cmd += device(driver="virtio-blk-pci-non-transitional", bus="pci.7", addr="0x0", drive="root", id="ua-root",
                      bootindex=1)
        cmd += blockdev("seed", self.seed, "raw", True)
        cmd += device(driver="virtio-blk-pci-non-transitional", bus="pci.8", addr="0x0", drive="seed", id="ua-seed")
        cmd += ["-netdev", f"user,id=hostnet0,hostfwd=tcp:127.0.0.1:{self.ssh_port}-:22,"
                           f"hostfwd=tcp:127.0.0.1:{self.metrics_port}-:9100"]
        cmd += device(driver="virtio-net-pci-non-transitional", netdev="hostnet0", id="ua-default", mac=self.mac,
                      bus="pci.1", addr="0x0", romfile="")
        cmd += ["-chardev", f"socket,id=charchannel0,path={self.agent.path},server=on,wait=off"]
        cmd += device(driver="virtserialport", bus="virtio-serial0.0", nr=1, chardev="charchannel0", id="channel0",
                      name="org.qemu.guest_agent.0")
        cmd += ["-chardev", f"file,id=charserial0,path={self.serial_log}"]
        if self.arch == "x86_64":
            cmd += device(driver="isa-serial", chardev="charserial0", id="serial0", index=0)
            cmd += device(driver="bochs-display", id="video0", bus="pcie.0", addr="0x1")
        else:
            cmd += ["-serial", "chardev:charserial0"]
        cmd += device(driver="virtio-balloon-pci-non-transitional", id="balloon0", bus="pci.9", addr="0x0")
        cmd += ["-object", json.dumps({"qom-type": "rng-random", "id": "objrng0", "filename": "/dev/urandom"})]
        cmd += device(driver="virtio-rng-pci-non-transitional", rng="objrng0", id="rng0", bus="pci.10", addr="0x0")
        cmd += ["-qmp", f"unix:{os.path.join(self.sockets, 'qmp')},server=on,wait=off"]
        return cmd

    def start(self) -> None:
        with open(os.path.join(self.workdir, "qemu.log"), "w") as output:
            self.process = subprocess.Popen(self.command(), stdout=output, stderr=subprocess.STDOUT,
                                            stdin=subprocess.DEVNULL)

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def wait_agent(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.alive():
                with open(os.path.join(self.workdir, "qemu.log"), encoding="utf-8", errors="replace") as handle:
                    raise MigrateError("QEMU exited: " + (handle.read().strip().splitlines() or ["no output"])[-1])
            if self.agent.ping():
                return True
            time.sleep(3)
        return False

    def stop(self) -> None:
        if not self.alive():
            return
        try:
            self.agent.command("guest-shutdown", {"mode": "powerdown"}, timeout=5)
        except AgentError:
            pass
        process = self.process
        if process is None:
            return
        try:
            process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def serial_tail(self, lines: int = 25) -> str:
        try:
            with open(self.serial_log, encoding="utf-8", errors="replace") as handle:
                return "\n".join(handle.read().splitlines()[-lines:])
        except OSError:
            return ""


class Ssh:
    def __init__(self, port: int, key: str, workdir: str):
        self.port, self.key, self.workdir = port, key, workdir
        self.options = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                        "-o", "ConnectTimeout=8", "-o", "IdentitiesOnly=yes", "-p", str(port)]

    def run(self, user: str, command: str, stdin: str | None = None, timeout: int = 120):
        cmd = ["ssh", "-o", "BatchMode=yes", "-i", self.key] + self.options + [f"{user}@127.0.0.1", command]
        try:
            return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, 255, "", "timed out")

    def wait(self, user: str, timeout: float):
        deadline = time.monotonic() + timeout
        result = None
        while time.monotonic() < deadline:
            result = self.run(user, "true", timeout=20)
            if result.returncode == 0:
                return result
            time.sleep(5)
        return result

    def password_login(self, user: str, password: str) -> bool:
        """A real password login: the ssh client reads the password from an askpass program."""
        askpass = os.path.join(self.workdir, "askpass.sh")
        with open(askpass, "w", encoding="utf-8") as handle:
            handle.write(f"#!/bin/sh\necho '{password}'\n")
        os.chmod(askpass, 0o700)
        env = dict(os.environ, SSH_ASKPASS=askpass, SSH_ASKPASS_REQUIRE="force", DISPLAY="none")
        cmd = ["ssh", "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
               "-o", "NumberOfPasswordPrompts=1"] + self.options + [f"{user}@127.0.0.1", "true"]
        try:
            return subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=40).returncode == 0
        except subprocess.TimeoutExpired:
            return False


def parse_probe(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key] = value.strip()
    return values


def check_agent(machine: Machine, checks: Checks, suffix: str = "") -> None:
    try:
        info = machine.agent.command("guest-info")
    except AgentError as err:
        checks.add(f"agent-rpcs{suffix}", "fail", str(err))
        return
    enabled = {command["name"] for command in info["supported_commands"] if command["enabled"]}
    missing = [name for name in REQUIRED_RPCS if name not in enabled]
    open_rpcs = [name for name in FORBIDDEN_RPCS if name in enabled]
    detail = f"qemu-ga {info.get('version', '?')}"
    if missing:
        detail += f"; disabled: {', '.join(missing)}"
    if open_rpcs:
        detail += f"; not closed: {', '.join(open_rpcs)}"
    checks.expect(f"agent-rpcs{suffix}", not missing and not open_rpcs, detail)
    for name in ("guest-get-osinfo", "guest-get-fsinfo", "guest-get-host-name", "guest-get-users", "guest-get-timezone"):
        try:
            machine.agent.command(name)
        except AgentError as err:
            checks.add(f"agent-reads{suffix}", "fail", str(err))
            return
    checks.add(f"agent-reads{suffix}", "pass", "os, filesystem, host name, user and time zone reads answer")


def check_nic(machine: Machine, checks: Checks, name: str) -> None:
    """The pod NIC carries the platform's name and a DHCP address; the lease may take a moment."""
    deadline = time.monotonic() + 120
    detail = ""
    while True:
        try:
            interfaces = machine.agent.command("guest-network-get-interfaces")
            mine = [nic for nic in interfaces if nic.get("hardware-address", "").lower() == machine.mac]
            if mine:
                addresses = [ip["ip-address"] for ip in mine[0].get("ip-addresses", []) if ip["ip-address-type"] == "ipv4"]
                detail = f"{mine[0]['name']} {' '.join(addresses) or 'without an IPv4 address'}"
                if mine[0]["name"] == POD_NIC and addresses:
                    checks.add(name, "pass", detail)
                    return
            else:
                detail = "the guest reports no interface with the pod NIC's MAC address"
        except AgentError as err:
            detail = str(err)
        if time.monotonic() > deadline:
            checks.add(name, "fail", detail)
            return
        time.sleep(4)


def check_metrics(machine: Machine, checks: Checks, name: str) -> None:
    body = ""
    for _ in range(10):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{machine.metrics_port}/metrics", timeout=10) as response:
                body = response.read().decode("utf-8", errors="replace")
            break
        except OSError:
            time.sleep(3)
    checks.expect(name, "node_memory_MemTotal_bytes" in body and "node_memory_MemAvailable_bytes" in body,
                  "the memory series answer on 9100/tcp from outside the guest" if body else "nothing answers on 9100/tcp")


def boot_checks(machine: Machine, ssh: Ssh, checks: Checks, manifest: dict, suffix: str = "") -> dict:
    """The required checks every boot passes. Returns the probe's values, empty when it could not run."""
    check_agent(machine, checks, suffix)
    check_nic(machine, checks, f"pod-nic{suffix}")
    login = ssh.wait(VERIFY_USER, 300)
    if not checks.expect(f"ssh-key{suffix}", login is not None and login.returncode == 0,
                         f"{VERIFY_USER} logs in with the key from the NoCloud disk" if login and login.returncode == 0
                         else f"key login failed: {(login.stderr.strip() if login else 'no attempt')[-200:]}"):
        return {}
    ssh.run(VERIFY_USER, "sudo cloud-init status --wait >/dev/null 2>&1 || true", timeout=600)
    script = (PROBE.replace("__NIC__", POD_NIC).replace("__TOOLS__", " ".join(SCRATCH_TOOLS))
              .replace("__PWUSER__", PASSWORD_USER))
    result = ssh.run(VERIFY_USER, "sudo sh -s", stdin=script)
    probe = parse_probe(result.stdout)
    if not probe:
        checks.add(f"probe{suffix}", "fail", f"the probe did not run: {result.stderr.strip()[-200:]}")
        return {}

    checks.expect(f"uefi{suffix}", probe.get("efi") == "yes", "booted through UEFI" if probe.get("efi") == "yes" else "not a UEFI boot")
    checks.expect(f"root-on-virtio{suffix}", probe.get("root_disks") == "vda",
                  f"/ is {probe.get('root_source')} on {probe.get('root_disks') or 'an unknown disk'}")
    status = probe.get("ci_status", "")
    checks.expect(f"cloud-init{suffix}", "done" in status and probe.get("ci_datasource", "").startswith("DataSourceNoCloud")
                  and probe.get("marker") == "yes",
                  f"{status or 'no status'}; {probe.get('ci_datasource') or 'no datasource'}; "
                  f"user script {'ran' if probe.get('marker') == 'yes' else 'did not run'}")
    if probe.get("ci_errors") not in ("", "0", None):
        checks.add(f"cloud-init-errors{suffix}", "restricted", f"cloud-init recorded {probe['ci_errors']} module error(s)")
    checks.expect(f"hostname{suffix}", probe.get("hostname") == machine.hostname, probe.get("hostname", ""))
    try:
        agent_host = machine.agent.command("guest-get-host-name").get("host-name", "")
    except AgentError as err:
        agent_host = str(err)
    checks.expect(f"agent-hostname{suffix}", agent_host == machine.hostname, f"the guest agent reports {agent_host}")
    routes = [route for route in probe.get("default_routes", "").split(";") if route]
    checks.expect(f"default-route{suffix}", len(routes) == 1 and f"dev {POD_NIC}" in routes[0], "; ".join(routes) or "none")
    checks.expect(f"pod-nic-address{suffix}", probe.get("nic_pci") == "0000:01:00.0",
                  f"PCI {probe.get('nic_pci')}, path name {probe.get('nic_name_path') or '-'}, "
                  f"slot name {probe.get('nic_name_slot') or '-'}, interfaces: {probe.get('nics')}")
    wanted = {"enforcing": "Enforcing", "permissive": "Permissive"}.get(manifest["selinux"], "Disabled")
    checks.expect(f"selinux{suffix}", probe.get("selinux") == wanted, f"{probe.get('selinux')} (the disk is configured {manifest['selinux']})")
    check_metrics(machine, checks, f"node-exporter{suffix}")
    return probe


def first_boot_checks(machine: Machine, ssh: Ssh, checks: Checks, manifest: dict, password: str, extra: str | None) -> dict:
    probe = boot_checks(machine, ssh, checks, manifest)
    if not probe:
        return {}
    pw_ok = ssh.password_login(PASSWORD_USER, password)
    checks.expect("password-user", pw_ok,
                  "the platform's password user logs in" if pw_ok else
                  f"password login fails ({probe.get('ci_version', 'cloud-init')}; shadow entry "
                  f"{'set' if probe.get('password_hash', '').startswith('$') else 'not set'})", soft=True)
    tail_mib = int(probe.get("disk_tail_sectors") or 0) * 512 >> 20
    before, now = int(manifest.get("root_fs_bytes") or 0), int(probe.get("root_fs_bytes") or 0)
    grown = before > 0 and now - before >= GROW_BYTES * 3 // 4
    checks.expect("root-growth", grown,
                  f"the root filesystem grew by {(now - before) >> 20} MiB into the larger disk" if grown else
                  f"/ ({probe.get('root_source')}) grew by {max(now - before, 0) >> 20} MiB of the {GROW_BYTES >> 20} MiB "
                  f"added; {tail_mib} MiB of the disk stay unallocated: grow it by hand after resizing", soft=True)
    scratch = not probe.get("scratch_missing") and probe.get("lvm_autoactivation") != "0" and probe.get("lvm_devicesfile") != "0"
    checks.expect("local-scratch", scratch,
                  "the tools of the platform's local scratch volume are present" if scratch else
                  ("missing: " + probe["scratch_missing"] if probe.get("scratch_missing") else
                   "this lvm2 lacks --setautoactivation/--devicesfile") + "; use raw-nvme flavors only", soft=True)
    if probe.get("failed_units"):
        checks.add("failed-units", "info", probe["failed_units"])
    if manifest["selinux"] != "disabled" and "--> off" in probe.get("qga_bool", ""):
        checks.add("agent-freeze-boolean", "restricted",
                   "virt_qemu_ga_read_nonsecurity_files is off: snapshots cannot freeze filesystems under an enforcing policy")
    if extra:
        with open(extra, encoding="utf-8") as handle:
            outcome = ssh.run(VERIFY_USER, "sudo sh -s", stdin=handle.read())
        output = (outcome.stdout + outcome.stderr).strip().splitlines()
        checks.expect("extra", outcome.returncode == 0, f"{extra}: {len(output)} lines of output" if outcome.returncode == 0
                      else "; ".join(output[-8:]))
    return probe


def reboot(machine: Machine, ssh: Ssh, boot_id: str, timeout: float) -> str:
    """Reboot the guest and wait until it answers from another boot. Returns what was observed."""
    ssh.run(VERIFY_USER, "sudo systemctl reboot", timeout=30)
    deadline = time.monotonic() + timeout
    seen = "the guest did not answer again"
    while time.monotonic() < deadline:
        if not machine.alive():
            return "QEMU exited"
        if machine.agent.ping():
            result = ssh.run(VERIFY_USER, "cat /proc/sys/kernel/random/boot_id", timeout=20)
            current = result.stdout.strip()
            if result.returncode == 0 and current:
                if current != boot_id:
                    return ""
                seen = "the guest kept answering from the same boot"
        time.sleep(4)
    return seen


def run_verification(disk: str, manifest: dict, workdir: str) -> dict:
    checks = Checks()
    arch = manifest["os"]["arch"]
    timeout = int(setting("VERIFY_TIMEOUT", "900"))
    extra = setting("VERIFY_EXTRA") or None
    key = os.path.join(workdir, "id_ed25519")
    run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "pmig-verify", "-f", key], capture=True)
    with open(f"{key}.pub", encoding="utf-8") as handle:
        key_pub = handle.read().strip()
    password = "Pm1g-" + uuid.uuid4().hex[:12]
    serial_logs = []
    machine_ids = []
    machines = []
    try:
        for index in (1, 2):
            log(f"cold boot {index} on a fresh overlay and firmware variable store")
            machine = Machine(disk, workdir, arch, index, key_pub, password)
            machines.append(machine)
            serial_logs.append(machine.serial_log)
            ssh = Ssh(machine.ssh_port, key, machine.workdir)
            machine.start()
            name = "boot" if index == 1 else "second-instance"
            if not machine.wait_agent(timeout):
                checks.add(name, "fail", f"the guest agent did not answer within {timeout}s; the serial console ends with:\n"
                           + machine.serial_tail())
                break
            checks.add(name, "pass", "the guest agent answers")
            if index == 1:
                probe = first_boot_checks(machine, ssh, checks, manifest, password, extra)
                machine_ids.append(probe.get("machine_id", ""))
                if checks.failed:
                    break
                log("rebooting the guest")
                problem = reboot(machine, ssh, probe.get("boot_id", ""), timeout)
                if not checks.expect("reboot", not problem, problem or "the guest answers from a new boot"):
                    break
                boot_checks(machine, ssh, checks, manifest, "-after-reboot")
            else:
                probe = boot_checks(machine, ssh, checks, manifest, "-second-instance")
                if probe:
                    machine_ids.append(probe.get("machine_id", ""))
                    distinct = len(machine_ids) == 2 and all(machine_ids) and machine_ids[0] != machine_ids[1]
                    checks.expect("machine-id", distinct, "each instance has its own machine-id" if distinct
                                  else f"instances share or lack a machine-id: {machine_ids}")
            machine.stop()
    finally:
        for machine in machines:
            process = machine.process
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            shutil.rmtree(machine.sockets, ignore_errors=True)
    qemu = run([first_existing(arch_info(arch)["qemu"], "QEMU"), "--version"], capture=True).stdout.splitlines()[0]
    return {
        "passed": not checks.failed,
        "checks": checks.items,
        "restrictions": [item for item in checks.items if item["status"] == "restricted"],
        "machine": f"{arch_info(arch)['machine']} of {qemu}; the platform's machine type version may differ",
        "serial_logs": serial_logs,
    }


def verify_disk(disk: str, manifest: dict, dest: str) -> dict:
    """Verify a disk its manifest describes. The result and the serial consoles go to dest; nothing is raised
    for a guest that fails: the result says so."""
    workdir = tempfile.mkdtemp(prefix=".verify.", dir=dest)
    started = time.time()
    try:
        try:
            result = run_verification(disk, manifest, workdir)
        except (MigrateError, OSError) as err:
            result = {"passed": False, "restrictions": [], "serial_logs": [],
                      "checks": [{"name": "verification", "status": "fail", "detail": str(err)}]}
        for index, path in enumerate(result.pop("serial_logs"), 1):
            if os.path.exists(path):
                shutil.copyfile(path, os.path.join(dest, f"serial-{index}.log"))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    result.update({"disk_sha256": manifest["disk"]["sha256"], "name": manifest["name"],
                   "duration_seconds": int(time.time() - started),
                   "verified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return result


def failure(result: dict) -> str:
    failed = [item for item in result["checks"] if item["status"] == "fail"]
    return "verification failed: " + "; ".join(
        f"{item['name']} ({item['detail'].splitlines()[0] if item['detail'] else 'failed'})" for item in failed)


def summary(result: dict) -> str:
    restricted = result["restrictions"]
    return (f"verified {result['name']}: every required check passed"
            + (f"; restricted: {', '.join(item['name'] for item in restricted)}" if restricted else ""))


def verify_output(out: str) -> dict:
    """Verify output/<name>/disk.qcow2 again and record the result next to it."""
    with locked(out):
        for stale in glob.glob(os.path.join(out, ".verify.*")):
            shutil.rmtree(stale, ignore_errors=True)
        disk = os.path.join(out, "disk.qcow2")
        manifest_path = os.path.join(out, "manifest.json")
        record = os.path.join(out, "verify.json")
        if not os.path.isfile(disk) or not os.path.isfile(manifest_path):
            raise MigrateError(f"{out} holds no converted disk (run migrate first)")
        manifest = read_json(manifest_path)
        # An earlier result must not outlive a run that does not finish.
        if os.path.exists(record):
            os.unlink(record)
        if sha256_file(disk) != manifest["disk"]["sha256"]:
            raise MigrateError(f"{disk} is not the disk its manifest describes")
        result = verify_disk(disk, manifest, out)
        write_json(record, result)
    if not result["passed"]:
        raise MigrateError(failure(result))
    log(summary(result))
    return result
