import unittest

from pmig.inspect import ESP_GUID, LINUX_GUID, LVM_GUID, SWAP_GUID
from pmig.relocate import ESP_BYTES, MIB, plan_layout, sgdisk_args
from pmig.util import MigrateError

GIB = 1 << 30


def part(num, start, size, kind="data", ptype=LINUX_GUID, **extra):
    return {"num": num, "start": start, "end": start + size - 1, "size": size, "kind": kind, "type": ptype, **extra}


def disk(parts, size=10 * GIB, parttype="msdos", **extra):
    return {"size": size, "sector_size": 512, "parttype": parttype, "partitions": parts, "disk_guid": None, **extra}


class Layout(unittest.TestCase):
    def test_single_partition_mbr_filling_the_disk(self):
        source = disk([part(1, MIB, 10 * GIB - MIB)])
        layout = plan_layout(source)
        root, esp = layout["partitions"]
        self.assertEqual((esp["num"], esp["start"], esp["end"]), (2, MIB, MIB + ESP_BYTES - 1))
        self.assertEqual(esp["type"], ESP_GUID)
        self.assertEqual((root["num"], root["start"]), (1, MIB + ESP_BYTES))
        self.assertEqual(root["end"] - root["start"], 10 * GIB - MIB - 1)
        # Room for the backup partition table after the last partition, on a whole-MiB disk.
        self.assertGreaterEqual(layout["size"] - root["end"] - 1, 34 * 512)
        self.assertEqual(layout["size"] % MIB, 0)

    def test_logical_partitions_keep_their_numbers_and_the_extended_one_is_dropped(self):
        source = disk([
            part(1, MIB, 512 * MIB),
            part(2, 513 * MIB, 4 * GIB, kind="extended"),
            part(5, 514 * MIB, GIB, ptype=SWAP_GUID),
            part(6, 514 * MIB + GIB + MIB, 2 * GIB, ptype=LVM_GUID),
        ])
        layout = plan_layout(source)
        self.assertEqual([entry["num"] for entry in layout["partitions"]], [1, 5, 6, 7])
        self.assertEqual(layout["esp"]["num"], 7)
        self.assertEqual([entry["type"] for entry in layout["partitions"]], [LINUX_GUID, SWAP_GUID, LVM_GUID, ESP_GUID])
        for entry, original in zip(layout["partitions"], [source["partitions"][0]] + source["partitions"][2:]):
            self.assertEqual(entry["start"] - original["start"], layout["shift"])
            self.assertEqual(entry["end"] - entry["start"], original["end"] - original["start"])

    def test_old_63_sector_alignment_still_clears_the_esp(self):
        source = disk([part(1, 63 * 512, GIB)])
        layout = plan_layout(source)
        self.assertGreater(layout["partitions"][0]["start"], layout["esp"]["end"])
        self.assertEqual(layout["shift"] % MIB, 0)

    def test_gpt_identity_is_carried_over(self):
        source = disk([
            part(1, MIB, MIB, kind="bios_boot", ptype="21686148-6449-6E6F-744E-656564454649", guid="A" * 8 + "-0000-0000-0000-000000000001", name="bios", attributes=0),
            part(4, 2 * MIB, GIB, guid="B" * 8 + "-0000-0000-0000-000000000004", name="root", attributes=1 << 60),
        ], parttype="gpt", disk_guid="C" * 8 + "-0000-0000-0000-00000000000C")
        layout = plan_layout(source)
        args = sgdisk_args(layout)
        self.assertIn(f"--disk-guid={'C' * 8}-0000-0000-0000-00000000000C", args)
        self.assertIn(f"--partition-guid=4:{'B' * 8}-0000-0000-0000-000000000004", args)
        self.assertIn("--change-name=4:root", args)
        self.assertIn("--attributes=4:set:60", args)
        self.assertIn(f"--typecode=5:{ESP_GUID}", args)
        self.assertEqual(layout["esp"]["num"], 5)

    def test_an_existing_fat_partition_becomes_the_esp_in_place(self):
        source = disk([part(1, MIB, 512 * MIB), part(2, 513 * MIB, GIB, kind="extended"), part(5, 514 * MIB, GIB - MIB)])
        layout = plan_layout(source, esp_source=1)
        self.assertEqual(layout["shift"], 0)
        self.assertEqual([entry["num"] for entry in layout["partitions"]], [1, 5])
        self.assertEqual(layout["esp"]["num"], 1)
        self.assertEqual(layout["partitions"][0]["type"], ESP_GUID)
        self.assertEqual(layout["partitions"][0]["source"], 1)

    def test_refusals(self):
        with self.assertRaises(MigrateError):
            plan_layout(disk([part(1, MIB, GIB)], sector_size=4096))
        with self.assertRaises(MigrateError):
            plan_layout(disk([part(1, MIB, GIB, kind="extended")]))
        with self.assertRaises(MigrateError):
            plan_layout(disk([part(1, MIB, GIB), part(2, MIB + GIB - 512, GIB)]))


if __name__ == "__main__":
    unittest.main()
