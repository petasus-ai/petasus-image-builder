#!/bin/sh
# Install what the platform needs and the guest lacks. Nothing else is upgraded, except cloud-init
# older than the release that reads the platform's password users.
. ./lib.sh

REPO_DIR=$PM_DIR/repos
MEDIA=$PM_DIR/media
# cloud-init reads the password users of the platform's user-data from this release on.
CLOUD_INIT_FLOOR=22.3

cleanup() {
    if grep -qs " $MEDIA " /proc/mounts; then umount "$MEDIA" || umount -l "$MEDIA"; fi
    [ "${POLICY_RC:-}" = ours ] && rm -f /usr/sbin/policy-rc.d
    return 0
}
trap cleanup EXIT

# Build-time repositories: an installation DVD attached as a disk, or the operator's repository file.
if [ -n "${PM_MEDIA_DEV:-}" ]; then
    [ "$FAMILY" = el ] || fail repos "an installation DVD is a RHEL-family source"
    mkdir -p "$MEDIA" "$REPO_DIR"
    # The appliance may still be probing the drive right after it starts.
    mount -o ro "$PM_MEDIA_DEV" "$MEDIA" || { sleep 5 && mount -o ro "$PM_MEDIA_DEV" "$MEDIA"; } ||
        fail repos "the installation DVD does not mount"
    keys=$(for key in /etc/pki/rpm-gpg/RPM-GPG-KEY-*; do [ -f "$key" ] && printf 'file://%s ' "$key"; done)
    [ -n "$keys" ] || fail repos "the guest has no RPM signing keys under /etc/pki/rpm-gpg to check the DVD's packages with"
    : >"$REPO_DIR/media.repo"
    if [ -d "$MEDIA/BaseOS" ]; then trees="BaseOS AppStream"; else trees=.; fi
    for tree in $trees; do
        [ -d "$MEDIA/$tree/repodata" ] || fail repos "the DVD has no repository at $tree"
        case "$tree" in .) id=root ;; *) id=$(printf '%s' "$tree" | tr 'A-Z' 'a-z') ;; esac
        printf '[petasus-media-%s]\nname=installation media %s\nbaseurl=file://%s/%s\nenabled=1\ngpgcheck=1\ngpgkey=%s\n\n' \
            "$id" "$tree" "$MEDIA" "$tree" "$keys" >>"$REPO_DIR/media.repo"
    done
fi

if [ "$FAMILY" = el ]; then
    if have dnf; then PM=dnf; else PM=yum; fi
    # Metadata and downloaded packages stay in the scratch directory, off the guest's disk.
    PM_OPTS="-y --setopt=timeout=30 --setopt=retries=3 --setopt=cachedir=$PM_DIR/cache --setopt=keepcache=0"
    [ "$PM" = dnf ] && PM_OPTS="$PM_OPTS --setopt=install_weak_deps=False"
    if [ -d "$REPO_DIR" ] && ls "$REPO_DIR"/*.repo >/dev/null 2>&1; then
        # The RHEL subscription plugins would record the build repositories as the products' sources.
        PM_OPTS="$PM_OPTS --setopt=reposdir=$REPO_DIR --disableplugin=subscription-manager,product-id"
        info repos "packages come from the build repositories only"
    fi
    pm_install() { $PM $PM_OPTS install "$@"; }
    pm_reinstall() { $PM $PM_OPTS reinstall "$@"; }
    pm_upgrade() { $PM $PM_OPTS upgrade "$@"; }
else
    # No prompts, and no restart advice from needrestart: it would wait for a key press.
    export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1
    # Package lists and downloaded packages stay in the scratch directory, off the guest's disk.
    mkdir -p "$PM_DIR/cache/lists/partial" "$PM_DIR/cache/archives/partial"
    APT_OPTS="-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::Retries=2 -o Acquire::ForceIPv4=true -o APT::Sandbox::User=root -o Dir::State::lists=$PM_DIR/cache/lists -o Dir::Cache::archives=$PM_DIR/cache/archives"
    if [ -d "$REPO_DIR" ] && ls "$REPO_DIR"/build.* >/dev/null 2>&1; then
        source_file=$(ls "$REPO_DIR"/build.* | head -n 1)
        APT_OPTS="$APT_OPTS -o Dir::Etc::sourcelist=$source_file -o Dir::Etc::sourceparts=/dev/null"
        info repos "packages come from the build repositories only"
    fi
    # Package scripts must not start services: nothing of the guest runs here.
    if [ ! -e /usr/sbin/policy-rc.d ]; then
        printf '#!/bin/sh\nexit 101\n' >/usr/sbin/policy-rc.d
        chmod 755 /usr/sbin/policy-rc.d
        POLICY_RC=ours
    fi
    APT_UPDATED=
    apt_update() {
        [ -n "$APT_UPDATED" ] && return 0
        # shellcheck disable=SC2086
        apt-get $APT_OPTS update && APT_UPDATED=1
    }
    # shellcheck disable=SC2086
    pm_install() { apt_update && apt-get $APT_OPTS --no-install-recommends install "$@"; }
    # shellcheck disable=SC2086
    pm_reinstall() { apt_update && apt-get $APT_OPTS --no-install-recommends install --reinstall "$@"; }
    # shellcheck disable=SC2086
    pm_upgrade() { apt_update && apt-get $APT_OPTS --no-install-recommends install --only-upgrade "$@"; }
fi

# retry COMMAND...: package mirrors fail transiently.
retry() {
    _try=1
    while ! "$@"; do
        [ "$_try" -ge 3 ] && return 1
        _try=$((_try + 1))
        sleep 10
    done
}

if [ "$FAMILY" = el ]; then
    wanted="cloud-init cloud-utils-growpart qemu-guest-agent lvm2 xfsprogs openssh-server sudo dosfstools"
    # growpart before 0.31 grows a GPT partition with sgdisk; releases from 10 no longer carry gdisk.
    [ "${OS_MAJOR:-0}" -lt 10 ] && wanted="$wanted gdisk"
    [ "${OS_MAJOR:-0}" -ge 8 ] && wanted="$wanted NetworkManager"
    efi_packages="grub2-efi-$EFI_ARCH shim-$EFI_ARCH"
else
    wanted="cloud-init cloud-guest-utils qemu-guest-agent lvm2 xfsprogs openssh-server sudo dosfstools"
    # cloud-init renders the platform network through netplan from Ubuntu 18.04, through ifupdown before.
    [ "$OS_ID" = ubuntu ] && [ "${OS_MAJOR:-0}" -ge 18 ] && wanted="$wanted netplan.io"
    efi_packages="grub-efi-$DEB_ARCH"
    if [ "${PM_EFI_INSTALL:-0}" = 1 ]; then
        # The loader goes to the removable-media path too, now and at every later grub update:
        # the platform's firmware keeps no boot entries.
        printf '%s\n' "grub-efi-$DEB_ARCH grub2/force_efi_extra_removable boolean true" \
            "grub-efi-$DEB_ARCH grub2/update_nvram boolean false" | debconf-set-selections
    fi
fi

missing=
for package in $wanted; do
    pkg_installed "$package" || missing="$missing $package"
done
reinstall=
if [ "${PM_EFI_INSTALL:-0}" = 1 ]; then
    for package in $efi_packages; do
        if pkg_installed "$package"; then reinstall="$reinstall $package"; else missing="$missing $package"; fi
    done
fi

HINT="if the guest's own repositories are unreachable, give BUILD_REPO_FILE or EL_REPO_ISO"
if [ -n "$missing$reinstall" ]; then
    free_kb=$(df -Pk / | awk 'NR==2 {print $4}')
    [ "${free_kb:-0}" -ge 102400 ] ||
        fail packages "the guest's root filesystem has $((free_kb / 1024)) MiB free; installing$missing$reinstall needs room"
fi
if [ -n "$missing" ]; then
    info packages "installing:$missing"
    # shellcheck disable=SC2086
    retry pm_install $missing || fail packages "could not install:$missing ($HINT)"
else
    info packages "every package the platform needs is installed"
fi
if [ -n "$reinstall" ] && [ "$FAMILY" = el ]; then
    # The packages carry the loader files; reinstalling puts them on the new EFI system partition.
    # shellcheck disable=SC2086
    retry pm_reinstall $reinstall || fail packages "could not reinstall:$reinstall ($HINT)"
fi
case " $missing " in *" openssh-server "*) : >"$PM_DIR/installed-openssh-server" ;; esac

ci_version() { cloud-init --version 2>&1 | sed -n 's/.* \([0-9][0-9]*\.[0-9][0-9.]*\).*/\1/p' | head -n 1; }
version=$(ci_version)
if [ -n "$version" ] && version_lt "$version" "$CLOUD_INIT_FLOOR"; then
    info packages "cloud-init $version predates $CLOUD_INIT_FLOOR; trying the newest the repositories carry"
    retry pm_upgrade cloud-init || true
    version=$(ci_version)
fi
if [ -z "$version" ]; then
    fail packages "cloud-init does not run in this guest"
elif version_lt "$version" "$CLOUD_INIT_FLOOR"; then
    restrict password-users "cloud-init $version ignores the password users the platform sets (chpasswd.users needs $CLOUD_INIT_FLOOR); SSH keys work"
fi
info packages "cloud-init $version"
exit 0
