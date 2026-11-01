"""
The eras modal's list by kind (1 Nov 2026, the user: "eras should be grouped by type and ordered by start
date. solar and batteries should be close together in the groupings"; newest first within a group).

The list was every era newest first, kinds mixed. Now a heading a kind, in ERA_KINDS' order — Solar, Battery,
Heat pump, Air-to-air, EV, Insulation, House move, Meter change, Other — and each kind's eras newest first.
A kind not known goes with Other.

Checked in the local preview on 1 Nov 2026: Solar, Battery (the bigger battery above the one it replaced),
EV (the newer car first), Insulation.

Discriminating: every test fails on the parent tree.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()


def _fn(name):
    i = PAGE.index("function " + name + "(")
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheGroups(unittest.TestCase):

    def _run(self, eras):
        i = PAGE.index("var ERA_KINDS = {")
        kinds = PAGE[i:PAGE.index("\n};\n", i) + 3]
        js = kinds + "\n" + _fn("erasGroups") + "\nconsole.log(JSON.stringify(erasGroups(%s).map(function (g) { " \
             "return [g.kind, g.eras.map(function (e) { return e.id; })]; })));" % json.dumps(eras)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_by_kind_solar_and_battery_first_newest_first(self):
        eras = [{"id": "car2", "kind": "ev", "start": "2026-09-21"},
                {"id": "bat2", "kind": "battery", "start": "2025-08-20"},
                {"id": "pv", "kind": "solar", "start": "2024-07-14"},
                {"id": "bat1", "kind": "battery", "start": "2024-07-14"},
                {"id": "car1", "kind": "ev", "start": "2023-09-21"},
                {"id": "hp", "kind": "heat_pump", "start": "2022-03-01"}]
        self.assertEqual(self._run(eras), [["solar", ["pv"]], ["battery", ["bat2", "bat1"]],
                                           ["heat_pump", ["hp"]], ["ev", ["car2", "car1"]]])

    def test_a_kind_not_known_goes_with_other(self):
        eras = [{"id": "x", "kind": "windmill", "start": "2024-01-01"},
                {"id": "o", "kind": "other", "start": "2025-01-01"}]
        self.assertEqual(self._run(eras), [["other", ["o", "x"]]])

    def test_the_list_draws_a_heading_a_kind(self):
        self.assertIn("el.innerHTML = erasGroups(_eras).map(function(g) {", _fn("erasRenderList"))
        self.assertIn("'<div class=\"era-group\">'", _fn("erasRenderList"))


if __name__ == "__main__":
    unittest.main()
