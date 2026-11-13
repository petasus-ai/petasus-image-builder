#!/bin/sh
# Services the platform talks to: cloud-init, sshd, the guest agent, the node exporter, and the
# guest firewall's opening for the exporter.
. ./lib.sh

# --- cloud-init -------------------------------------------------------------------------------
for unit in cloud-init-local.service cloud-init.service cloud-init-main.service cloud-init-network.service \
    cloud-config.service cloud-final.service; do
    if [ -L "/etc/systemd/system/$unit" ] && [ "$(readlink "/etc/systemd/system/$unit")" = /dev/null ]; then
        rm -f "/etc/systemd/system/$unit"
        info cloud-init "unmasked $unit"
    fi
    for dir in /usr/lib/systemd/system /lib/systemd/system; do
        if [ -f "$dir/$unit" ] && grep -q '^\[Install\]' "$dir/$unit"; then
            systemctl enable "$unit" >/dev/null 2>&1 || warn cloud-init "could not enable $unit"
            break
        fi
    done
done

# --- sshd -------------------------------------------------------------------------------------
if [ "$FAMILY" = el ]; then ssh_unit=sshd.service; else ssh_unit=ssh.service; fi
if [ -e "$PM_DIR/installed-openssh-server" ]; then
    systemctl enable "$ssh_unit" >/dev/null 2>&1 || warn sshd "could not enable $ssh_unit"
elif ! unit_enabled "$ssh_unit" && ! unit_enabled ssh.socket && ! unit_enabled sshd.socket; then
    warn sshd "$ssh_unit is installed but not enabled; the platform's key pairs log in over SSH"
fi

# --- guest agent ------------------------------------------------------------------------------
# The platform's policy (ALLOW_RPCS, BLOCK_RPCS): inventory, freeze and power RPCs stay; the RPCs
# that run commands, read or write files, or change credentials from the host side are closed.
qga=$(command -v qemu-ga || true)
[ -n "$qga" ] || fail guest-agent "qemu-ga is not installed"
qga_help=$("$qga" --help 2>&1 || true)
case "$qga_help" in
    *--allow-rpcs*) qga_filter=allow ;;
    *--block-rpcs*) qga_filter=block ;;
    *) qga_filter=blacklist ;;
esac

if [ -f /etc/sysconfig/qemu-ga ]; then
    # RHEL family: the unit reads its arguments from this file.
    unit_text=$(cat /usr/lib/systemd/system/qemu-guest-agent.service /etc/systemd/system/qemu-guest-agent.service 2>/dev/null)
    backup_copy /etc/sysconfig/qemu-ga
    case "$unit_text" in
        *FILTER_RPC_ARGS*)
            if [ "$qga_filter" = allow ]; then value="--allow-rpcs=$ALLOW_RPCS"; else value="--block-rpcs=$BLOCK_RPCS"; fi
            variable=FILTER_RPC_ARGS
            ;;
        *BLACKLIST_RPC*)
            value=$BLOCK_RPCS
            variable=BLACKLIST_RPC
            ;;
        *) fail guest-agent "the qemu-guest-agent unit takes no RPC filter from /etc/sysconfig/qemu-ga" ;;
    esac
    if grep -q "^$variable=" /etc/sysconfig/qemu-ga; then
        sed -i "s|^$variable=.*|$variable=\"$value\"|" /etc/sysconfig/qemu-ga
    else
        printf '%s="%s"\n' "$variable" "$value" >>/etc/sysconfig/qemu-ga
    fi
    info guest-agent "RPC filter set through $variable"
else
    # Debian family: the agent reads /etc/qemu/qemu-ga.conf; the unit is static and udev starts it.
    mkdir -p /etc/qemu /etc/qemu/fsfreeze-hook.d
    backup_copy /etc/qemu/qemu-ga.conf
    {
        echo "[general]"
        case "$qga_filter" in
            allow) echo "allow-rpcs=$ALLOW_RPCS" ;;
            block) echo "block-rpcs=$BLOCK_RPCS" ;;
            *) echo "blacklist=$BLOCK_RPCS" ;;
        esac
        [ -x /etc/qemu/fsfreeze-hook ] && echo "fsfreeze-hook=/etc/qemu/fsfreeze-hook"
    } >/etc/qemu/qemu-ga.conf
    chmod 644 /etc/qemu/qemu-ga.conf
    info guest-agent "RPC filter set in /etc/qemu/qemu-ga.conf ($qga_filter list)"
fi
for dir in /usr/lib/systemd/system /lib/systemd/system; do
    if [ -f "$dir/qemu-guest-agent.service" ] && grep -q '^\[Install\]' "$dir/qemu-guest-agent.service"; then
        systemctl enable qemu-guest-agent.service >/dev/null 2>&1 || warn guest-agent "could not enable qemu-guest-agent"
        break
    fi
done
if [ -L /etc/systemd/system/qemu-guest-agent.service ] && [ "$(readlink /etc/systemd/system/qemu-guest-agent.service)" = /dev/null ]; then
    rm -f /etc/systemd/system/qemu-guest-agent.service
    info guest-agent "unmasked qemu-guest-agent.service"
fi
qga_version=$("$qga" --version 2>&1 | sed -n 's/.*[Aa]gent \([0-9][0-9.]*\).*/\1/p' | head -n 1)
if [ -n "$qga_version" ] && version_lt "$qga_version" 5.1; then
    # Before 5.1 the agent reports the host name it read at its start: it starts after cloud-init sets it.
    mkdir -p /etc/systemd/system/qemu-guest-agent.service.d
    printf '[Unit]\nAfter=cloud-init.service\n' >/etc/systemd/system/qemu-guest-agent.service.d/50-petasus-after-cloud-init.conf
    info guest-agent "qemu-ga $qga_version starts after cloud-init sets the host name"
fi

# Under an enforcing policy the agent may freeze and trim mounted filesystems only with this boolean,
# and the SSH key boolean serves RPCs that are closed.
set_boolean() {
    # set_boolean NAME 0|1: record the value in the policy store; no policy is loaded here.
    _type=$(sed -n 's/^SELINUXTYPE=//p' /etc/selinux/config | head -n 1)
    _store=/var/lib/selinux/${_type:-targeted}/active
    [ -d "$_store" ] || _store=/etc/selinux/${_type:-targeted}/modules/active
    [ -d "$_store" ] || return 1
    { [ -f "$_store/booleans.local" ] && grep -v "^$1=" "$_store/booleans.local"; echo "$1=$2"; } >"$_store/booleans.new" &&
        mv "$_store/booleans.new" "$_store/booleans.local"
}
if [ "${PM_SELINUX:-disabled}" != disabled ] && [ "$FAMILY" = el ]; then
    if have semanage && semanage boolean -N -m --on virt_qemu_ga_read_nonsecurity_files >/dev/null 2>&1 &&
        semanage boolean -N -m --off virt_qemu_ga_manage_ssh >/dev/null 2>&1; then
        info guest-agent "SELinux booleans for the guest agent set"
    elif set_boolean virt_qemu_ga_read_nonsecurity_files 1 && set_boolean virt_qemu_ga_manage_ssh 0 &&
        semodule -n -B >/dev/null 2>&1; then
        info guest-agent "SELinux booleans for the guest agent set in the policy store"
    else
        warn guest-agent "SELinux booleans not set; run: setsebool -P virt_qemu_ga_read_nonsecurity_files=on virt_qemu_ga_manage_ssh=off"
    fi
fi

# --- node exporter ----------------------------------------------------------------------------
if [ -n "$NODE_EXPORTER" ]; then
    info node-exporter "$NODE_EXPORTER is already enabled in the guest and keeps serving port 9100"
else
    install -m 0755 "$PM_DIR/node_exporter" /usr/local/bin/node_exporter
    if ! getent passwd nodeusr >/dev/null 2>&1; then
        nologin=$(command -v nologin || echo /bin/false)
        useradd --system --user-group --no-create-home --shell "$nologin" --comment "node exporter" nodeusr ||
            fail node-exporter "could not create the nodeusr account"
    fi
    install -m 0644 "$PM_DIR/node_exporter.service" /etc/systemd/system/node_exporter.service
    systemctl enable node_exporter.service >/dev/null 2>&1 || fail node-exporter "could not enable node_exporter.service"
    info node-exporter "installed $("/usr/local/bin/node_exporter" --version 2>&1 | head -n 1)"
fi

# --- guest firewall ---------------------------------------------------------------------------
# The platform scrapes the exporter on 9100/tcp; a firewall carried over from the source allows SSH at most.
if have firewall-offline-cmd && unit_enabled firewalld.service; then
    firewall-offline-cmd --add-port=9100/tcp >/dev/null 2>&1
    if firewall-offline-cmd --query-port=9100/tcp >/dev/null 2>&1; then
        info firewall "firewalld: 9100/tcp open in the default zone"
    else
        warn firewall "firewalld: could not open 9100/tcp"
    fi
fi
if have ufw && grep -qs '^ENABLED=yes' /etc/ufw/ufw.conf; then
    # With ENABLED=yes ufw would also load the rule into the running kernel, which is not the guest's.
    sed -i 's/^ENABLED=yes/ENABLED=no/' /etc/ufw/ufw.conf
    if ufw prepend allow 9100/tcp >/dev/null 2>&1 || ufw insert 1 allow 9100/tcp >/dev/null 2>&1 ||
        ufw allow 9100/tcp >/dev/null 2>&1; then
        info firewall "ufw: 9100/tcp allowed"
    else
        warn firewall "ufw: could not allow 9100/tcp"
    fi
    sed -i 's/^ENABLED=no/ENABLED=yes/' /etc/ufw/ufw.conf
fi
for unit in nftables.service iptables.service netfilter-persistent.service; do
    if [ "$(systemctl is-enabled "$unit" 2>/dev/null)" = enabled ]; then
        warn firewall "$unit loads the guest's own rule set; it must accept 9100/tcp for the platform's monitoring"
    fi
done
exit 0
