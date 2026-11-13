#!/bin/sh
# Last step: an initramfs that boots from virtio-blk on any hardware, and a UEFI boot loader the
# firmware finds without boot entries.
. ./lib.sh

DRIVERS="virtio_blk virtio_scsi virtio_pci"
# Room one new initramfs needs on /boot while the one it replaces is still there.
BOOT_HEADROOM_KB=131072

kernel_list=$(kernels)
[ -n "$kernel_list" ] || fail boot "no kernel with an image under /boot"

# The initramfs is assembled in the scratch directory: the guest's own filesystems may have no room.
TMPDIR=$PM_DIR/tmp
export TMPDIR
mkdir -p "$TMPDIR"

FAKE_EFI=
cleanup() {
    [ -n "$FAKE_EFI" ] && umount /sys/firmware 2>/dev/null
    return 0
}
trap cleanup EXIT

# failed FILE MESSAGE: show the tool's output, then fail.
failed() {
    cat "$1"
    fail boot "$2: $(tail -n 3 "$1" | tr '\n' ' ')"
}

has_driver() {
    # has_driver KVER IMAGE: the driver is built in or in the initramfs.
    grep -qs "/virtio_blk\.ko" "/lib/modules/$1/modules.builtin" && return 0
    if have lsinitrd; then
        lsinitrd "$2" 2>/dev/null | grep -q 'virtio_blk\.ko'
    else
        lsinitramfs "$2" 2>/dev/null | grep -q 'virtio_blk\.ko'
    fi
}

boot_room() {
    _avail=$(df -Pk /boot | awk 'NR==2 {print $4}')
    [ "${_avail:-0}" -ge "$BOOT_HEADROOM_KB" ] ||
        fail boot "/boot has $((_avail / 1024)) MiB free; a hardware-independent initramfs needs $((BOOT_HEADROOM_KB / 1024)) MiB"
}

if [ "$PM_INITRAMFS" = dracut ]; then
    mkdir -p /etc/dracut.conf.d
    {
        echo "# The initramfs must not depend on the machine it was built on: the disk is virtio-blk here."
        echo 'hostonly="no"'
        echo "add_drivers+=\" $DRIVERS \""
        echo "install_items+=\" $POD_LINK \""
    } >/etc/dracut.conf.d/90-petasus-platform.conf
    for kver in $kernel_list; do
        if [ -e "/boot/initrd.img-$kver" ] || [ "$FAMILY" = deb ]; then
            image=/boot/initrd.img-$kver
        else
            image=/boot/initramfs-$kver.img
        fi
        boot_room
        dracut --force --tmpdir "$TMPDIR" --kver "$kver" "$image" >"$PM_DIR/dracut.err" 2>&1 ||
            failed "$PM_DIR/dracut.err" "dracut failed for kernel $kver"
        has_driver "$kver" "$image" || fail boot "the initramfs of kernel $kver has no virtio_blk driver"
        info boot "initramfs rebuilt for kernel $kver"
    done
else
    for module in $DRIVERS; do
        grep -qx "$module" /etc/initramfs-tools/modules 2>/dev/null || echo "$module" >>/etc/initramfs-tools/modules
    done
    if grep -rqs '^MODULES=dep' /etc/initramfs-tools/initramfs.conf /etc/initramfs-tools/conf.d; then
        # `dep` would fit the initramfs to the conversion host's hardware.
        echo "MODULES=most" >/etc/initramfs-tools/conf.d/zz-petasus-platform.conf
    fi
    for kver in $kernel_list; do
        boot_room
        if [ -e "/boot/initrd.img-$kver" ]; then mode=-u; else mode=-c; fi
        update-initramfs "$mode" -k "$kver" >"$PM_DIR/initramfs.err" 2>&1 ||
            failed "$PM_DIR/initramfs.err" "update-initramfs failed for kernel $kver"
        has_driver "$kver" "/boot/initrd.img-$kver" || fail boot "the initramfs of kernel $kver has no virtio_blk driver"
        info boot "initramfs rebuilt for kernel $kver"
    done
fi

# --- boot loader ------------------------------------------------------------------------------
mountpoint -q /boot/efi || fail boot "no EFI system partition is mounted at /boot/efi"

# GRUB's scripts write BIOS or UEFI boot commands by whether /sys/firmware/efi exists, and here that
# is the conversion appliance's firmware, not the platform's.
if [ ! -d /sys/firmware/efi ]; then
    mount -t tmpfs tmpfs /sys/firmware && mkdir /sys/firmware/efi && FAKE_EFI=1 ||
        fail boot "cannot present a UEFI environment to the boot loader tools"
fi

if [ "$FAMILY" = el ]; then
    vendor=
    for dir in /boot/efi/EFI/*/; do
        name=$(basename "$dir")
        case "$name" in BOOT | boot | Boot) ;; *) [ -e "$dir/shim$EFI_ARCH.efi" ] || [ -e "$dir/grub$EFI_ARCH.efi" ] && vendor=$name ;; esac
    done
    [ -n "$vendor" ] || fail boot "no boot loader directory under /boot/efi/EFI (grub2-efi-$EFI_ARCH and shim-$EFI_ARCH put one there)"
    config=/boot/efi/EFI/$vendor/grub.cfg
    if [ -d /boot/loader/entries ] && [ "${OS_MAJOR:-0}" -ge 9 ]; then
        if [ "${PM_EFI_INSTALL:-0}" = 1 ] || [ ! -s "$config" ]; then
            # The ESP holds a stub that hands over to the configuration on /boot.
            uuid=$(grub2-probe --target=fs_uuid /boot/grub2) || fail boot "cannot find the filesystem of /boot/grub2"
            relative=$(grub2-mkrelpath /boot/grub2)
            {
                echo "search --no-floppy --fs-uuid --set=dev $uuid"
                echo "set prefix=(\$dev)$relative"
                echo "export \$prefix"
                echo "configfile \$prefix/grub.cfg"
            } >"$config"
            chmod 700 "$config"
            grub2-mkconfig -o /boot/grub2/grub.cfg >"$PM_DIR/grub.err" 2>&1 || failed "$PM_DIR/grub.err" "grub2-mkconfig failed"
            info boot "wrote the GRUB stub $config"
        fi
    elif [ "${PM_EFI_INSTALL:-0}" = 1 ] || [ ! -d /boot/loader/entries ]; then
        # RHEL 7 and 8 read the whole configuration, and their environment block, from the ESP.
        env=/boot/efi/EFI/$vendor/grubenv
        if [ -f /boot/grub2/grubenv ] && [ ! -L /boot/grub2/grubenv ]; then
            cp /boot/grub2/grubenv "$env"
            ln -sf "../efi/EFI/$vendor/grubenv" /boot/grub2/grubenv
        fi
        grub2-mkconfig -o "$config" >"$PM_DIR/grub.err" 2>&1 || failed "$PM_DIR/grub.err" "grub2-mkconfig failed"
        info boot "generated $config"
    fi
else
    if [ "${PM_EFI_INSTALL:-0}" = 1 ]; then
        grub-install --target="$GRUB_TARGET" --efi-directory=/boot/efi --no-nvram --force-extra-removable \
            >"$PM_DIR/grub.err" 2>&1 ||
            grub-install --target="$GRUB_TARGET" --efi-directory=/boot/efi --no-nvram --removable \
                >>"$PM_DIR/grub.err" 2>&1 ||
            failed "$PM_DIR/grub.err" "grub-install failed"
        info boot "installed GRUB for UEFI, with the removable-media path"
    fi
    update-grub >"$PM_DIR/grub.err" 2>&1 || failed "$PM_DIR/grub.err" "update-grub failed"
fi

# The firmware keeps no boot entries, so it starts the loader at the removable-media path.
fallback=
for file in /boot/efi/EFI/BOOT/* /boot/efi/EFI/boot/* /boot/efi/EFI/Boot/*; do
    [ -f "$file" ] && [ "$(basename "$file" | tr 'a-z' 'A-Z')" = "$EFI_BOOT" ] && fallback=$file
done
[ -n "$fallback" ] || fail boot "the EFI system partition has no EFI/BOOT/$EFI_BOOT"
info boot "UEFI fallback loader: $fallback"
exit 0
