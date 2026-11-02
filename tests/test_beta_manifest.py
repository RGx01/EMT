"""
The add-on's beta manifest (2 Nov 2026, the user: "slug emt, name as you suggested"; "CalVer with b betas, keep the
port as v4 does").

The repository carried the soak build's manifest — "Energy Meter Tracker SOAK", slug `emt_soak`, version
`4.5.23-a24` — and `repository.json` and the image label pointed at the v4 repository. Now: slug `emt` (v4's is
`energy_meter_tracker`, so the two run side by side), name "EMT — Energy Meter Tracker", CalVer `2026.11.0b1`, the
sidebar "EMT", the direct port published on 8098 as before (v4's is 8099), and every link to this repository. The
version both readers take from config.yaml is the CalVer one, and the import gate places it above the v4 floor.

Discriminating: every test fails on the parent tree but test_the_port_is_as_v4_has_it, a GUARD.
"""
import json
import os
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _read(*p):
    return open(os.path.join(_ROOT, *p), encoding="utf-8").read()


CFG = _read("config.yaml")


def _key(k):
    m = re.search(r'^%s:\s*"?([^"\n]*)"?' % k, CFG, re.M)
    return m.group(1) if m else None


class TheManifest(unittest.TestCase):

    def test_slug_name_version(self):
        self.assertEqual(_key("slug"), "emt")
        self.assertEqual(_key("name"), "EMT — Energy Meter Tracker")
        self.assertRegex(_key("version"), r"^\d{4}\.\d{1,2}\.\d+(b\d+)?$")
        self.assertEqual(_key("panel_title"), "EMT")

    def test_the_port_is_as_v4_has_it(self):
        # GUARD: passes on the parent tree too — the direct port stays published (8098)
        self.assertIn("ports:\n  8099/tcp: 8098", CFG)
        self.assertIn("ingress: true", CFG)

    def test_every_link_is_this_repository(self):
        self.assertEqual(json.loads(_read("repository.json"))["url"], "https://github.com/RGx01/EMT")
        self.assertIn('"https://github.com/RGx01/EMT"', _read("build.yaml"))
        self.assertIn("'https://github.com/RGx01/EMT/issues'", _read("web", "templates", "meter_config.html"))
        self.assertIn('_RELEASES_API = "https://api.github.com/repos/RGx01/EMT/releases"', _read("web", "server.py"))

    def test_the_version_readers_and_the_gate(self):
        import engine
        from v4import import gate
        v = engine._read_config_version()
        self.assertEqual(v, _key("version"))
        self.assertGreater(gate.parse_version(v), gate.parse_version(gate.FLOOR_VERSION))


if __name__ == "__main__":
    unittest.main()
