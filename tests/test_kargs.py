import unittest

from pmig.kargs import edit_args, edit_bls_entry, edit_grub_defaults, edit_kernelopts


class EditArgs(unittest.TestCase):
    def test_names_and_cloud_init_switches_go(self):
        args = "ro net.ifnames=0 biosdevname=0 cloud-init=disabled network-config=disabled ds=nocloud quiet"
        self.assertEqual(edit_args(args, "ttyS0", ensure=False), "ro quiet")

    def test_enabled_cloud_init_stays(self):
        self.assertEqual(edit_args("cloud-init=enabled", "ttyS0", ensure=False), "cloud-init=enabled")

    def test_additions(self):
        self.assertEqual(edit_args("ro quiet", "ttyS0"),
                         "ro quiet console=tty0 console=ttyS0,115200n8 modprobe.blacklist=nouveau,amdgpu nouveau.modeset=0")

    def test_an_existing_serial_console_is_kept(self):
        out = edit_args("console=tty1 console=ttyS0 ro", "ttyS0")
        self.assertEqual(out.count("console="), 2)
        self.assertTrue(out.startswith("console=tty1 console=ttyS0 ro"))

    def test_serial_console_is_added_after_another_console(self):
        self.assertIn("console=tty0 ro console=ttyAMA0,115200n8", edit_args("console=tty0 ro", "ttyAMA0"))

    def test_blacklists_merge(self):
        out = edit_args("modprobe.blacklist=floppy,nouveau ro nouveau.modeset=1", "ttyS0")
        self.assertIn("modprobe.blacklist=floppy,nouveau,amdgpu nouveau.modeset=0", out)
        self.assertEqual(out.count("modprobe.blacklist="), 1)
        self.assertNotIn("nouveau.modeset=1", out)

    def test_idempotent(self):
        once = edit_args("ro net.ifnames=0", "ttyS0")
        self.assertEqual(edit_args(once, "ttyS0"), once)


class GrubDefaults(unittest.TestCase):
    def test_el_defaults(self):
        files = {"/etc/default/grub": 'GRUB_TIMEOUT=1\nGRUB_CMDLINE_LINUX="console=ttyS0,115200n8 no_timer_check net.ifnames=0 crashkernel=auto"\n'}
        text = edit_grub_defaults(files, "ttyS0")["/etc/default/grub"]
        self.assertIn('GRUB_CMDLINE_LINUX="console=ttyS0,115200n8 no_timer_check crashkernel=auto '
                      'modprobe.blacklist=nouveau,amdgpu nouveau.modeset=0"', text)
        self.assertIn("GRUB_TIMEOUT=1\n", text)

    def test_ubuntu_cloud_image_keeps_its_console_in_the_default_variable(self):
        files = {
            "/etc/default/grub": 'GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"\nGRUB_CMDLINE_LINUX=""\n',
            "/etc/default/grub.d/50-cloudimg-settings.cfg": 'GRUB_CMDLINE_LINUX_DEFAULT="console=tty1 console=ttyS0"\n',
        }
        changed = edit_grub_defaults(files, "ttyS0")
        self.assertEqual(list(changed), ["/etc/default/grub"])
        self.assertIn('GRUB_CMDLINE_LINUX="modprobe.blacklist=nouveau,amdgpu nouveau.modeset=0"', changed["/etc/default/grub"])

    def test_a_later_file_decides_the_variable(self):
        files = {
            "/etc/default/grub": 'GRUB_CMDLINE_LINUX="net.ifnames=0"\n',
            "/etc/default/grub.d/99-site.cfg": "GRUB_CMDLINE_LINUX='biosdevname=0 audit=1'\n",
        }
        changed = edit_grub_defaults(files, "ttyS0")
        self.assertEqual(changed["/etc/default/grub"], 'GRUB_CMDLINE_LINUX=""\n')
        self.assertIn("GRUB_CMDLINE_LINUX='audit=1 console=tty0 console=ttyS0,115200n8 "
                      "modprobe.blacklist=nouveau,amdgpu nouveau.modeset=0'", changed["/etc/default/grub.d/99-site.cfg"])

    def test_a_file_without_the_variable_gains_it(self):
        changed = edit_grub_defaults({"/etc/default/grub": "GRUB_TIMEOUT=5\n"}, "ttyS0")
        self.assertIn('GRUB_CMDLINE_LINUX="console=tty0 console=ttyS0,115200n8 modprobe.blacklist=nouveau,amdgpu nouveau.modeset=0"',
                      changed["/etc/default/grub"])

    def test_a_trailing_comment_stays(self):
        changed = edit_grub_defaults({"/etc/default/grub": 'GRUB_CMDLINE_LINUX="quiet net.ifnames=0" # storage\n'}, "ttyS0")
        self.assertRegex(changed["/etc/default/grub"], r'^GRUB_CMDLINE_LINUX="quiet console=tty0 [^"]*" # storage\n$')

    def test_a_computed_value_stays_and_the_additions_follow_it(self):
        text = 'GRUB_CMDLINE_LINUX="$(cat /etc/kernel/cmdline)"\nGRUB_TIMEOUT=1\n'
        lines = edit_grub_defaults({"/etc/default/grub": text}, "ttyS0")["/etc/default/grub"].splitlines()
        self.assertEqual(lines[0], 'GRUB_CMDLINE_LINUX="$(cat /etc/kernel/cmdline)"')
        self.assertTrue(lines[1].startswith('GRUB_CMDLINE_LINUX="$GRUB_CMDLINE_LINUX console=tty0 '))
        self.assertEqual(lines[2], "GRUB_TIMEOUT=1")

    def test_escaped_quotes_stay_inside_the_value(self):
        text = 'GRUB_CMDLINE_LINUX="rd.lvm.lv=vg/root acpi_osi=\\"Windows 2015\\" net.ifnames=0"\n'
        out = edit_grub_defaults({"/etc/default/grub": text}, "ttyS0")["/etc/default/grub"]
        self.assertTrue(out.startswith('GRUB_CMDLINE_LINUX="rd.lvm.lv=vg/root acpi_osi=\\"Windows 2015\\" console=tty0 '), out)
        self.assertEqual(out.count("GRUB_CMDLINE_LINUX="), 1)

    def test_done_twice_changes_nothing(self):
        files = {"/etc/default/grub": 'GRUB_CMDLINE_LINUX="ro"\n'}
        files.update(edit_grub_defaults(files, "ttyS0"))
        self.assertEqual(edit_grub_defaults(files, "ttyS0"), {})


class Bls(unittest.TestCase):
    def test_literal_options_are_edited(self):
        entry = "title Rocky\nlinux /vmlinuz\noptions root=UUID=x ro net.ifnames=0\n"
        self.assertIn("options root=UUID=x ro console=tty0 console=ttyS0,115200n8", edit_bls_entry(entry, "ttyS0"))

    def test_variable_options_are_left_to_the_environment_block(self):
        entry = "title Rocky\noptions $kernelopts $tuned_params\n"
        self.assertEqual(edit_bls_entry(entry, "ttyS0"), entry)

    def test_literal_arguments_beside_variables_are_removed(self):
        entry = "options $kernelopts cloud-init=disabled net.ifnames=0 $tuned_params\n"
        self.assertEqual(edit_bls_entry(entry, "ttyS0"), "options $kernelopts $tuned_params\n")

    def test_kernelopts(self):
        out = edit_kernelopts("# GRUB Environment Block\nsaved_entry=x\nkernelopts=root=UUID=x ro biosdevname=0\n", "ttyS0")
        self.assertIn("kernelopts=root=UUID=x ro console=tty0 console=ttyS0,115200n8 modprobe.blacklist", out)
        self.assertIn("saved_entry=x\n", out)


if __name__ == "__main__":
    unittest.main()
