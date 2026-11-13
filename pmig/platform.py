"""What the platform expects of a guest: the pod NIC, the guest agent policy, the exporter."""
from __future__ import annotations

POD_NIC = "enp1s0"
POD_NIC_LINK = "/etc/systemd/network/70-petasus-pod-nic.link"
# What KubeVirt's virt-handler needs from the agent, and what a snapshot freezes with.
REQUIRED_RPCS = ("guest-ping", "guest-get-time", "guest-info", "guest-shutdown", "guest-network-get-interfaces",
                 "guest-get-fsinfo", "guest-get-host-name", "guest-get-users", "guest-get-timezone", "guest-get-osinfo",
                 "guest-fsfreeze-status", "guest-fsfreeze-freeze", "guest-fsfreeze-thaw")
# Inventory, freeze, power and resource RPCs; an agent that filters by allow-list opens only these.
ALLOWED_RPCS = REQUIRED_RPCS + (
    "guest-sync-delimited", "guest-sync", "guest-set-time", "guest-fsfreeze-freeze-list", "guest-fstrim",
    "guest-suspend-disk", "guest-suspend-ram", "guest-suspend-hybrid", "guest-get-vcpus", "guest-set-vcpus",
    "guest-get-disks", "guest-get-memory-blocks", "guest-set-memory-blocks", "guest-get-memory-block-info",
    "guest-get-devices", "guest-get-diskstats", "guest-get-cpustats", "guest-network-get-route", "guest-get-load")
# Commands, file access and credentials from the host side.
FORBIDDEN_RPCS = ("guest-exec", "guest-exec-status", "guest-file-open", "guest-file-close", "guest-file-read",
                  "guest-file-write", "guest-file-seek", "guest-file-flush", "guest-set-user-password",
                  "guest-ssh-get-authorized-keys", "guest-ssh-add-authorized-keys", "guest-ssh-remove-authorized-keys")
# Units under which a guest may already serve node_exporter's 9100/tcp.
EXPORTER_UNITS = ("node_exporter.service", "prometheus-node-exporter.service", "node-exporter.service")
