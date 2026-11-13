# shellcheck shell=sh
# Sourced by every guest script. They run as root inside the guest's root filesystem, under the
# conversion appliance's kernel: no service is running and `uname -r` is not the guest's kernel.
set -u

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH LC_ALL=C

# The conversion's settings and the guest's facts: PM_DIR, BACKUP, FAMILY (el or deb), OS_ID,
# OS_MAJOR, EFI_ARCH, EFI_BOOT, DEB_ARCH, GRUB_TARGET, POD_LINK, ALLOW_RPCS, BLOCK_RPCS,
# NODE_EXPORTER and the PM_* decisions.
# shellcheck disable=SC1091
. ./env
REPORT=$PM_DIR/report.tsv

note() {
    _level=$1 _key=$2
    shift 2
    printf '%s\t%s\t%s\n' "$_level" "$_key" "$*" >>"$REPORT"
    printf '%s %s: %s\n' "$_level" "$_key" "$*"
}
info() { note INFO "$@"; }
warn() { note WARN "$@"; }
restrict() { note RESTRICT "$@"; }
fail() {
    note FAIL "$@"
    exit 1
}
have() { command -v "$1" >/dev/null 2>&1; }

pkg_installed() {
    if [ "$FAMILY" = el ]; then
        rpm -q "$1" >/dev/null 2>&1
    else
        dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed'
    fi
}

# version_lt A B: A sorts before B.
version_lt() {
    [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n 1)" = "$1" ]
}

# Installed kernels the boot loader can start: a module tree and an image under /boot.
kernels() {
    for _dir in /lib/modules/*/; do
        _kver=$(basename "$_dir")
        [ -d "$_dir/kernel" ] && [ -e "/boot/vmlinuz-$_kver" ] && printf '%s\n' "$_kver"
    done
}

unit_enabled() {
    case "$(systemctl is-enabled "$1" 2>/dev/null)" in
        enabled | enabled-runtime | alias | static | indirect | generated) return 0 ;;
        *) return 1 ;;
    esac
}

backup_copy() {
    [ -e "$1" ] || return 0
    mkdir -p "$BACKUP$(dirname "$1")"
    [ -e "$BACKUP$1" ] || cp -a "$1" "$BACKUP$1"
}
