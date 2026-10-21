"""
The support file says what EMT runs on (21 Oct 2026, the user): `host` in support.json.

A helper reads the log's timings against the machine — a count that is 0.9 s on a Mac was 22 s on
the prod box, a thin client. From the platform, /proc and /sys where they exist, and the
Supervisor's /info (the board, the OS, the versions); nothing that names the machine (no hostname,
no address).

Discriminating: every test here fails on the parent.
"""
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import support_file as sf                      # noqa: E402

CPUINFO = "processor\t: 0\nmodel name\t: Intel(R) Pentium(R) Silver J5005 CPU @ 1.50GHz\n"
MEMINFO = "MemTotal:        8000000 kB\nMemAvailable:    5000000 kB\nSwapTotal:       0 kB\n"
MOUNTS = "overlay / overlay rw 0 0\n/dev/sda8 /data ext4 rw 0 0\n"


def _fake(files):
    def read(path, default=None):
        return files.get(path, default)
    return read


class TheMachine(unittest.TestCase):

    def _host(self, files, sup=None):
        with mock.patch.object(sf, "_read", _fake(files)):
            return sf.host_info("/data", sup)

    def test_processor_memory_and_disk(self):
        h = self._host({"/proc/cpuinfo": CPUINFO, "/proc/meminfo": MEMINFO, "/proc/mounts": MOUNTS,
                        "/sys/block/sda/queue/rotational": "0\n",
                        "/sys/block/sda/device/model": "SSD 128GB\n", "/proc/uptime": "7200.5 100\n"})
        self.assertEqual(h["cpu"]["model_name"], "Intel(R) Pentium(R) Silver J5005 CPU @ 1.50GHz")
        self.assertEqual(h["memory_mb"]["MemTotal"], 7812)
        self.assertEqual((h["disk"]["device"], h["disk"]["rotational"], h["disk"]["kind"]),
                         ("sda", False, "SSD or USB"))
        self.assertEqual(h["uptime_h"], 2.0)

    def test_a_pi_names_its_board_and_its_card(self):
        h = self._host({"/proc/cpuinfo": "Model\t\t: Raspberry Pi 4 Model B Rev 1.4\n",
                        "/proc/mounts": "/dev/mmcblk0p8 /data ext4 rw 0 0\n"})
        self.assertEqual(h["cpu"]["model"], "Raspberry Pi 4 Model B Rev 1.4")
        self.assertEqual(h["disk"]["kind"], "SD card or eMMC")

    def test_the_supervisor_without_the_hostname(self):
        h = self._host({}, {"arch": "amd64", "machine": "generic-x86-64", "hostname": "my-house",
                            "operating_system": "Home Assistant OS 16.2"})
        self.assertEqual(h["supervisor"]["machine"], "generic-x86-64")
        self.assertNotIn("my-house", repr(h))


class TheSummary(unittest.TestCase):

    def test_carries_the_host(self):
        with open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8") as f:
            src = f.read()
        fn = src[src.index("def _support_summary(store)"):src.index("def _supervisor_info()")]
        self.assertIn('("host", lambda: __import__("support_file").host_info(', fn)


if __name__ == "__main__":
    unittest.main()
