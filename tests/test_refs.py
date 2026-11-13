import unittest

from pmig.refs import Devices, edit_grubenv, rewrite_args, rewrite_fstab

PARTS = [
    {"num": 1, "uuid": "AAAA-BBBB", "guid": None},
    {"num": 2, "uuid": "11111111-2222-3333-4444-555555555555", "guid": None},
    {"num": 5, "uuid": "", "guid": None},
]
DEVICES = Devices(PARTS, uuids=["AAAA-BBBB", "11111111-2222-3333-4444-555555555555", "lv-uuid"],
                  labels=["cloudimg-rootfs"], paths=["/dev/vg0/root", "/dev/mapper/vg0-root"],
                  mbr_signature="1A2B3C4D", root_spec="UUID=11111111-2222-3333-4444-555555555555")
GPT = Devices([{"num": 1, "uuid": "u1", "guid": "0FC63DAF-0000-0000-0000-000000000001"}], uuids=["u1"],
              partlabels=["root"])
MANY = Devices([{"num": 10, "uuid": "ten"}, {"num": 16, "uuid": "sixteen"}], mbr_signature="deadbeef")


class Resolve(unittest.TestCase):
    def test_kernel_names_of_the_first_disk_become_uuids(self):
        for spec in ("/dev/sda2", "/dev/hda2", "/dev/xvda2", "/dev/vda2", "/dev/nvme0n1p2", "/dev/mmcblk0p2"):
            self.assertEqual(DEVICES.resolve(spec), ("rewrite", "UUID=11111111-2222-3333-4444-555555555555"), spec)

    def test_other_disks_and_absent_partitions_are_missing(self):
        for spec in ("/dev/sdb1", "/dev/nvme1n1p1", "/dev/nvme0n2p1", "/dev/sda9", "UUID=nope", "LABEL=nope", "/dev/vg1/data"):
            self.assertEqual(DEVICES.resolve(spec)[0], "missing", spec)

    def test_stable_references_stay(self):
        for spec in ("UUID=aaaa-bbbb", "LABEL=cloudimg-rootfs", "/dev/mapper/vg0-root", "/dev/vg0/root",
                     "/dev/disk/by-uuid/lv-uuid"):
            self.assertEqual(DEVICES.resolve(spec), ("present", None), spec)

    def test_a_partition_without_a_filesystem_uuid_keeps_its_name(self):
        self.assertEqual(DEVICES.resolve("/dev/sda5"), ("present", None))

    def test_mbr_partuuid_of_this_disk_becomes_a_uuid(self):
        for spec in ("PARTUUID=1a2b3c4d-02", "/dev/disk/by-partuuid/1a2b3c4d-02"):
            self.assertEqual(DEVICES.resolve(spec), ("rewrite", "UUID=11111111-2222-3333-4444-555555555555"), spec)

    def test_mbr_partuuid_of_another_disk_is_missing(self):
        self.assertEqual(DEVICES.resolve("PARTUUID=99999999-02"), ("missing", None))

    def test_mbr_partuuid_numbers_are_hexadecimal(self):
        self.assertEqual(MANY.resolve("PARTUUID=deadbeef-0a"), ("rewrite", "UUID=ten"))
        self.assertEqual(MANY.resolve("PARTUUID=deadbeef-10"), ("rewrite", "UUID=sixteen"))

    def test_names_the_source_hardware_gave_are_not_guessed(self):
        for spec in ("/dev/disk/by-path/pci-0000:00:10.0-scsi-0:0:0:0-part2", "/dev/disk/by-id/scsi-36000c29-part2",
                     "/dev/disk/by-id/wwn-0x5000c500a1b2c3d4"):
            self.assertEqual(DEVICES.resolve(spec), ("ambiguous", None), spec)

    def test_gpt_partuuid_survives(self):
        self.assertEqual(GPT.resolve("PARTUUID=0fc63daf-0000-0000-0000-000000000001"), ("present", None))
        self.assertEqual(GPT.resolve("PARTLABEL=root"), ("present", None))

    def test_what_is_not_a_disk_is_left_alone(self):
        for spec in ("tmpfs", "proc", "nfs.example:/export", "//server/share", "/swapfile", "/dev/sr0", "/dev/cdrom"):
            self.assertEqual(DEVICES.resolve(spec), ("opaque", None), spec)


class Fstab(unittest.TestCase):
    TEXT = (
        "# /etc/fstab\n"
        "/dev/sda2   /      xfs   defaults  0 0\n"
        "UUID=AAAA-BBBB /boot/efi vfat umask=0077 0 2\n"
        "/dev/sdb1\t/data\text4\tdefaults\t0 2\n"
        "/dev/sdc1 /backup ext4 defaults,nofail 0 2\n"
        "tmpfs /tmp tmpfs defaults 0 0\n"
    )

    def test_refuse_names_the_missing_mount(self):
        text, findings = rewrite_fstab(self.TEXT, DEVICES, "refuse")
        self.assertIn("UUID=11111111-2222-3333-4444-555555555555   /      xfs", text)
        actions = {finding["mount"]: finding["action"] for finding in findings}
        self.assertEqual(actions, {"/": "rewritten", "/data": "refused", "/backup": "ignored"})

    def test_nofail_marks_the_entry_and_keeps_the_rest(self):
        text, findings = rewrite_fstab(self.TEXT, DEVICES, "nofail")
        self.assertIn("/dev/sdb1\t/data\text4\tdefaults,nofail,x-systemd.device-timeout=30s\t0 2\n", text)
        self.assertIn("# /etc/fstab\n", text)
        self.assertIn("tmpfs /tmp tmpfs defaults 0 0\n", text)
        self.assertEqual([f["action"] for f in findings if f["mount"] == "/data"], ["nofail"])

    def test_a_missing_system_mount_is_refused_under_any_policy(self):
        for options in ("defaults", "defaults,nofail", "noauto"):
            _, findings = rewrite_fstab(f"/dev/sdb1 /var xfs {options} 0 0\n", DEVICES, "nofail")
            self.assertEqual(findings[0]["action"], "refused", options)

    def test_a_hardware_name_never_becomes_a_partition_of_this_disk(self):
        text = "/dev/disk/by-id/scsi-EXTERNAL-DATA-part2 /data xfs defaults 0 0\n"
        new, findings = rewrite_fstab(text, DEVICES, "refuse")
        self.assertEqual((new, findings[0]["action"]), (text, "refused"))
        new, findings = rewrite_fstab(text, DEVICES, "nofail")
        self.assertIn("scsi-EXTERNAL-DATA-part2 /data xfs defaults,nofail", new)

    def test_the_root_entry_takes_the_root_the_image_was_found_by(self):
        new, findings = rewrite_fstab("/dev/disk/by-id/scsi-36000c29-part2 / xfs defaults 0 0\n", DEVICES, "refuse")
        self.assertEqual(new, "UUID=11111111-2222-3333-4444-555555555555 / xfs defaults 0 0\n")
        self.assertEqual(findings[0]["action"], "rewritten")


class Args(unittest.TestCase):
    def test_root_and_resume(self):
        line = 'GRUB_CMDLINE_LINUX="root=/dev/sda2 ro resume=/dev/sda1 quiet"'
        self.assertEqual(rewrite_args(line, DEVICES),
                         'GRUB_CMDLINE_LINUX="root=UUID=11111111-2222-3333-4444-555555555555 ro resume=UUID=AAAA-BBBB quiet"')

    def test_stable_roots_are_untouched(self):
        line = "options root=/dev/mapper/vg0-root ro rd.lvm.lv=vg0/root"
        self.assertEqual(rewrite_args(line, DEVICES), line)

    def test_a_root_named_by_the_source_hardware_takes_the_known_root(self):
        self.assertEqual(rewrite_args("root=/dev/disk/by-path/pci-0000:03:00.0-part2 ro resume=/dev/disk/by-id/x-part1", DEVICES),
                         "root=UUID=11111111-2222-3333-4444-555555555555 ro resume=/dev/disk/by-id/x-part1")


class Grubenv(unittest.TestCase):
    def block(self, body: str) -> bytes:
        data = ("# GRUB Environment Block\n" + body).encode()
        return data + b"#" * (1024 - len(data))

    def test_size_is_kept(self):
        new = edit_grubenv(self.block("kernelopts=root=/dev/sda2 ro\n"), lambda text: rewrite_args(text, DEVICES))
        self.assertEqual(len(new), 1024)
        self.assertIn(b"kernelopts=root=UUID=11111111-2222-3333-4444-555555555555 ro\n#", new)

    def test_unchanged_block_is_returned_as_is(self):
        block = self.block("saved_entry=1\n")
        self.assertIs(edit_grubenv(block, lambda text: text), block)

    def test_overflow_is_an_error(self):
        with self.assertRaises(ValueError):
            edit_grubenv(self.block("a=b\n"), lambda text: text + "x" * 2000)


if __name__ == "__main__":
    unittest.main()
