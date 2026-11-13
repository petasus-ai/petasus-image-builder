import unittest

from pmig.cloudinit import dropin_name, missing_modules, strip_blocking


class StripBlocking(unittest.TestCase):
    def test_network_disable_file_goes_entirely(self):
        new, removed = strip_blocking("network: {config: disabled}\n")
        self.assertEqual(removed, ["network"])
        self.assertEqual(new.strip(), "#cloud-config")

    def test_other_keys_of_the_file_stay(self):
        text = "datasource_list: [None]\nnetwork:\n  config: disabled\nmanual_cache_clean: true\npreserve_hostname: true\n"
        new, removed = strip_blocking(text)
        self.assertEqual(removed, ["network", "manual_cache_clean"])
        self.assertIn("preserve_hostname: true", new)
        self.assertIn("datasource_list", new)
        self.assertNotIn("network", new)

    def test_a_pinned_system_network_config_goes(self):
        _, removed = strip_blocking("network:\n  version: 1\n  config:\n  - type: physical\n    name: eth0\n")
        self.assertEqual(removed, ["network"])

    def test_untouched_files(self):
        for text in ("users:\n- default\n", "# only a comment\n", "not: [valid", "- a list\n"):
            self.assertEqual(strip_blocking(text), (None, []), text)


class Dropin(unittest.TestCase):
    def test_sorts_last(self):
        self.assertEqual(dropin_name(["05_logging.cfg", "90_dpkg.cfg", "99-installer.cfg"]), "zz-petasus-platform.cfg")
        name = dropin_name(["zzz-site.cfg", "99-installer.cfg"])
        self.assertGreater(name, "zzz-site.cfg")

    def test_a_name_no_z_prefix_passes(self):
        name = dropin_name(["~override.cfg", "zz-petasus-platform.cfg"])
        self.assertEqual(name, "~~petasus-platform.cfg")
        self.assertGreater(name, "~override.cfg")
        self.assertEqual(dropin_name(["~override.cfg", name]), name)

    def test_the_name_stays_short(self):
        long_name = "zzz-" + "x" * 232 + ".cfg"
        self.assertEqual(dropin_name([long_name]), "~petasus-platform.cfg")

    def test_its_own_file_does_not_push_it_further(self):
        self.assertEqual(dropin_name(["zz-petasus-platform.cfg"]), "zz-petasus-platform.cfg")


class Modules(unittest.TestCase):
    def test_complete_configuration(self):
        merged = {
            "cloud_init_modules": ["bootcmd", "write_files", "growpart", "resizefs", "set_hostname", "update_etc_hosts",
                                   "users_groups", "ssh"],
            "cloud_config_modules": [["set-passwords", "always"], "runcmd"],
            "cloud_final_modules": ["scripts-user"],
        }
        self.assertEqual(missing_modules(merged), [])

    def test_trimmed_configuration(self):
        merged = {"cloud_init_modules": ["bootcmd", "ssh"], "cloud_config_modules": ["runcmd"]}
        missing = missing_modules(merged)
        self.assertIn("users-groups", missing)
        self.assertIn("set-passwords", missing)
        self.assertNotIn("runcmd", missing)

    def test_a_module_may_run_in_another_stage(self):
        merged = {"cloud_init_modules": ["bootcmd", "write-files", "growpart", "resizefs", "set-hostname",
                                         "update-etc-hosts", "users-groups", "ssh"],
                  "cloud_config_modules": ["set-passwords", "runcmd"], "cloud_final_modules": ["scripts-user"]}
        self.assertEqual(missing_modules(merged), [])


if __name__ == "__main__":
    unittest.main()
