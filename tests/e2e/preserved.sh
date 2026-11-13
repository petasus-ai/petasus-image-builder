#!/bin/sh
# VERIFY_EXTRA for the hostile fixtures: the workload of the source machine is still there, and the
# source's NIC configuration is set aside rather than lost. Runs as root in the booted guest.
rc=0
check() { # check DESCRIPTION COMMAND...
    _what=$1
    shift
    if "$@" >/dev/null 2>&1; then echo "ok: $_what"; else echo "MISSING: $_what"; rc=1; fi
}
check "the appsvc account" id appsvc
check "its password hash" sh -c 'getent shadow appsvc | cut -d: -f2 | grep -q "^\\$"'
check "its authorized key" grep -q source-admin /home/appsvc/.ssh/authorized_keys
check "the application data" grep -q "must survive" /srv/app/data.txt
check "the application log" grep -q "source machine" /var/log/app.log
check "the application service is enabled" systemctl is-enabled app-marker.service
check "the application service ran" systemctl is-active app-marker.service
check "the machine-id is not the source's" sh -c '! grep -q 0123456789abcdef0123456789abcdef /etc/machine-id'
check "the source's NIC configuration is in the backup directory" sh -c 'ls -R /var/backups/petasus-migrate | grep -Eq "ifcfg-eth0|00-installer-config.yaml"'
check "no address of the source environment is configured" sh -c '! ip -4 addr | grep -q "10\\.20\\.30\\."'
if [ -f /var/backups/petasus-migrate/etc/sysconfig/network-scripts/ifcfg-eth0 ]; then
    check "the dummy device profile stayed" test -f /etc/NetworkManager/system-connections/dummy-app.nmconnection
    check "the dummy device is up" ip link show dummy-app
    check "firewalld admits the node exporter" sh -c 'firewall-cmd --list-ports | grep -q 9100/tcp'
else
    check "the application bridge stayed" grep -q br-app /etc/netplan/00-installer-config.yaml
    check "ufw is active and admits the node exporter" sh -c 'ufw status | grep -q "^9100/tcp.*ALLOW"'
    check "the datasource of the source is not used" sh -c '! hostname | grep -q source-host'
fi
exit $rc
