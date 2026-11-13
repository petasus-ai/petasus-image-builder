#!/usr/bin/env bash
# Prepare the source disks the end-to-end runs convert: public cloud images as they are, and
# "hostile" variants that carry what a machine migrated from another environment typically does.
#
#   fixtures.sh DIR [NAME...]     names: see the case list below; default: all
#
# Needs Internet access, qemu-img and guestfs-tools. The rhel-* names, not in the default set, install
# from DIR/rhel-N-dvd.iso, which Red Hat supplies to its customers, and need qemu-kvm, edk2-ovmf and xorriso.
set -euo pipefail
DIR=${1:?usage: fixtures.sh DIR [NAME...]}
shift
SELF=$(readlink -f "$0")
export LIBGUESTFS_BACKEND=direct
mkdir -p "$DIR"
DIR=$(readlink -f "$DIR")
cd "$DIR"

fetch() { # fetch FILE URL
    [ -s "$1" ] && return 0
    curl -fL --retry 5 --retry-delay 5 -C - -o "$1.part" "$2"
    mv "$1.part" "$1"
}

# What a migrated workload leaves on its disk; tests/e2e/preserved.sh checks it after the conversion.
WORKLOAD=(
    --run-command 'useradd -m -s /bin/bash appsvc && echo "appsvc:Keep-This-Pw1" | chpasswd'
    --ssh-inject 'appsvc:string:ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureKeyOfTheSourceMachine00000000000000000 source-admin'
    --mkdir /srv/app
    --write '/srv/app/data.txt:workload data that must survive'
    --run-command 'setfattr -n user.app -v keep /srv/app/data.txt 2>/dev/null || true'
    --write '/var/log/app.log:a log line of the source machine'
    --write '/etc/systemd/system/app-marker.service:[Unit]
Description=Workload marker
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/true
[Install]
WantedBy=multi-user.target
'
    --run-command 'systemctl enable app-marker.service'
    --run-command 'echo 0123456789abcdef0123456789abcdef >/etc/machine-id'
)

build() { # build NAME BASE virt-customize-args...
    local name=$1 base=$2
    shift 2
    [ -s "$name.qcow2" ] && return 0
    cp --reflink=auto "$base" "$name.work"
    virt-customize -a "$name.work" "$@"
    mv "$name.work" "$name.qcow2"
}

installed() { # installed NAME DVD bios|uefi: an unattended minimal install from an installation DVD
    local name=$1 dvd=$2 firmware=$3 work label
    local firmware_args=()
    [ -s "$name.qcow2" ] && return 0
    [ -s "$dvd" ] || { echo "$DIR/$dvd is missing: put the installation DVD there" >&2; exit 2; }
    work=$(mktemp -d "$DIR/.$name.XXXXXX")
    label=$(blkid -p -s LABEL -o value "$dvd")
    xorriso -report_about SORRY -osirrox on -indev "$dvd" -extract /images/pxeboot/vmlinuz "$work/vmlinuz" \
        -extract /images/pxeboot/initrd.img "$work/initrd.img"
    # A server as an administrator installs one: LVM, a static address, firewalld, SELinux enforcing,
    # no cloud-init, not registered. The installer adds the guest agent of the hypervisor it runs on.
    cat >"$work/ks.cfg" <<'EOF'
text --non-interactive
cdrom
lang en_US.UTF-8
keyboard us
timezone Etc/UTC --utc
rootpw --plaintext fixture-root
network --device=link --bootproto=static --ip=10.20.30.42 --netmask=255.255.255.0 --gateway=10.20.30.1 --nameserver=10.20.30.2 --onboot=yes
selinux --enforcing
firewall --enabled --ssh
zerombr
clearpart --all --initlabel
autopart --type=lvm
poweroff
%packages
@^minimal-environment
%end
EOF
    xorriso -as mkisofs -quiet -V OEMDRV -o "$work/ks.iso" "$work/ks.cfg"
    qemu-img create -q -f qcow2 "$work/disk.qcow2" 20G
    if [ "$firmware" = uefi ]; then
        cp /usr/share/edk2/ovmf/OVMF_VARS.fd "$work/vars.fd"
        firmware_args=(-drive if=pflash,format=raw,readonly=on,file=/usr/share/edk2/ovmf/OVMF_CODE.secboot.fd
            -drive if=pflash,format=raw,file="$work/vars.fd")
    fi
    # The DVD is the first optical drive: the kickstart's `cdrom` installs from it.
    if ! timeout 3600 /usr/libexec/qemu-kvm -machine q35,accel=kvm -cpu host -smp 4 -m 4096 -display none -no-reboot \
        "${firmware_args[@]}" -serial file:"$work/console.log" \
        -kernel "$work/vmlinuz" -initrd "$work/initrd.img" \
        -append "inst.stage2=hd:LABEL=${label// /\\x20} inst.ks=hd:LABEL=OEMDRV:/ks.cfg inst.text console=ttyS0" \
        -drive file="$work/disk.qcow2",format=qcow2,if=virtio \
        -drive file="$dvd",format=raw,media=cdrom,readonly=on \
        -drive file="$work/ks.iso",format=raw,media=cdrom,readonly=on \
        -nic user,model=virtio-net-pci || ! virt-cat -a "$work/disk.qcow2" /etc/redhat-release; then
        tail -n 40 "$work/console.log" >&2
        echo "the installation of $name failed; see $work" >&2
        exit 1
    fi
    mv "$work/disk.qcow2" "$name.qcow2"
    rm -rf "$work"
}

all=(rocky-8 rocky-9 rocky-9-lvm rocky-10 ubuntu-2004 ubuntu-2204 ubuntu-2404 ubuntu-2604 centos-7 vb-ubuntu-2004
    vb-centos-78 rocky-9-dvd rocky-9-hostile ubuntu-2204-hostile)
[ $# -gt 0 ] || set -- "${all[@]}"

rocky=https://dl.rockylinux.org/pub/rocky
ubuntu=https://cloud-images.ubuntu.com/releases
for name in "$@"; do
    case $name in
        rocky-8) fetch $name.qcow2 $rocky/8/images/x86_64/Rocky-8-GenericCloud-Base.latest.x86_64.qcow2 ;;
        rocky-9) fetch $name.qcow2 $rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2 ;;
        rocky-9-lvm) fetch $name.qcow2 $rocky/9/images/x86_64/Rocky-9-GenericCloud-LVM.latest.x86_64.qcow2 ;;
        rocky-10) fetch $name.qcow2 $rocky/10/images/x86_64/Rocky-10-GenericCloud-Base.latest.x86_64.qcow2 ;;
        rocky-9-dvd) fetch $name.iso $rocky/9/isos/x86_64/Rocky-9-latest-x86_64-dvd.iso ;;
        ubuntu-2004) fetch $name.qcow2 $ubuntu/focal/release/ubuntu-20.04-server-cloudimg-amd64.img ;;
        ubuntu-2204) fetch $name.qcow2 $ubuntu/jammy/release/ubuntu-22.04-server-cloudimg-amd64.img ;;
        ubuntu-2404) fetch $name.qcow2 $ubuntu/noble/release/ubuntu-24.04-server-cloudimg-amd64.img ;;
        ubuntu-2604) fetch $name.qcow2 $ubuntu/resolute/release/ubuntu-26.04-server-cloudimg-amd64.img ;;
        centos-7) fetch $name.qcow2 https://cloud.centos.org/centos/7/images/CentOS-7-x86_64-GenericCloud-2211.qcow2 ;;
        # Installer-made disks: BIOS boot, no cloud-init.
        vb-ubuntu-2004) [ -s $name.qcow2 ] || virt-builder ubuntu-20.04 --format qcow2 -o $name.qcow2 --root-password password:fixture-root ;;
        vb-centos-78) [ -s $name.qcow2 ] || virt-builder centos-7.8 --format qcow2 -o $name.qcow2 --root-password password:fixture-root ;;
        # RHEL installed from its DVD on BIOS (RHEL 9: MBR, 10: GPT) or UEFI firmware.
        rhel-9-bios | rhel-9-uefi | rhel-10-bios | rhel-10-uefi) installed $name ${name%-*}-dvd.iso ${name##*-} ;;
        rocky-9-hostile)
            # No cloud-init and no guest agent, eth0-style names, a static address on the source's NIC,
            # device names in fstab and on the kernel command line, an initramfs fitted to other
            # hardware, a firewall that admits SSH only, and no reachable package repository.
            "$SELF" "$DIR" rocky-9
            build $name rocky-9.qcow2 \
                --install firewalld \
                --run-command 'dnf -y remove cloud-init qemu-guest-agent' \
                --run-command 'systemctl enable firewalld' \
                --run-command 'grubby --update-kernel=ALL --args="net.ifnames=0 biosdevname=0"' \
                --run-command 'sed -i "s/^GRUB_CMDLINE_LINUX=\"/&net.ifnames=0 biosdevname=0 /" /etc/default/grub' \
                --run-command 'root=$(findmnt -no SOURCE /); uuid=$(blkid -s UUID -o value "$root"); sed -i "s|^UUID=$uuid|$root|" /etc/fstab; grubby --update-kernel=ALL --args="root=$root"' \
                --write '/etc/sysconfig/network-scripts/ifcfg-eth0:DEVICE=eth0
TYPE=Ethernet
BOOTPROTO=static
IPADDR=10.20.30.40
PREFIX=24
GATEWAY=10.20.30.1
ONBOOT=yes
HWADDR=00:50:56:aa:bb:cc
' \
                --write '/etc/NetworkManager/system-connections/dummy-app.nmconnection:[connection]
id=dummy-app
type=dummy
interface-name=dummy-app
[ipv4]
method=manual
address1=192.0.2.77/32
' \
                --run-command 'chmod 600 /etc/NetworkManager/system-connections/dummy-app.nmconnection' \
                --run-command 'for k in /lib/modules/*/; do k=$(basename $k); [ -e /boot/vmlinuz-$k ] && dracut -f --hostonly --omit-drivers "virtio_blk virtio_scsi virtio_pci virtio_net" /boot/initramfs-$k.img $k; done' \
                --run-command 'sed -i "s/^enabled=1/enabled=0/" /etc/yum.repos.d/*.repo' \
                "${WORKLOAD[@]}" --selinux-relabel
            ;;
        ubuntu-2204-hostile)
            # cloud-init switched off and pinned to another datasource with networking disabled, a
            # static netplan for the source's NIC, a MAC-pinned interface name, eth0-style names,
            # and ufw enabled with SSH only.
            "$SELF" "$DIR" ubuntu-2204
            build $name ubuntu-2204.qcow2 \
                --write '/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg:network: {config: disabled}
' \
                --write '/etc/cloud/cloud.cfg.d/99-installer.cfg:datasource_list: [None]
datasource:
  None:
    metadata: {instance-id: source-instance}
    userdata_raw: "#cloud-config\nhostname: source-host\n"
' \
                --touch /etc/cloud/cloud-init.disabled \
                --run-command 'rm -f /etc/netplan/*.yaml' \
                --write '/etc/netplan/00-installer-config.yaml:network:
  version: 2
  ethernets:
    ens160:
      addresses: [10.20.30.41/24]
      routes:
        - to: default
          via: 10.20.30.1
      nameservers:
        addresses: [10.20.30.2]
  bridges:
    br-app:
      interfaces: []
      addresses: [172.31.250.1/24]
' \
                --write '/etc/udev/rules.d/70-persistent-net.rules:SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="00:50:56:aa:bb:dd", NAME="eth0"
' \
                --run-command 'sed -i "s/^GRUB_CMDLINE_LINUX=\"/&net.ifnames=0 biosdevname=0 /" /etc/default/grub && update-grub' \
                --run-command 'ufw allow 22/tcp && sed -i "s/^ENABLED=no/ENABLED=yes/" /etc/ufw/ufw.conf' \
                "${WORKLOAD[@]}"
            ;;
        *) echo "unknown fixture $name" >&2; exit 2 ;;
    esac
    echo "ready: $name"
done
