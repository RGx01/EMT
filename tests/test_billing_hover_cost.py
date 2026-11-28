"""
The Billing tab's day charts: the hover gives each bar's cost for the half-hour, in brackets after its kWh,
and the half-hour's total import with its cost as the hover's last line (28 Nov 2026, the user).

The chart's data already carries both (`meters[*].cost`, `ti_cost`, inc VAT); the hover showed kWh only. A
half-hour with no cost (not priced, or a meter without rates) shows none, rather than £0.00; a cost under a
penny is given to four places, as a rate is.

Discriminating: every test fails on the parent (`hoverLabel` is new; the hover was an inline callback that
printed kWh only) — the GUARD too, only because the function is new: the rate's line is unchanged.
"""
import json
import os
import shutil
import subprocess
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(_ROOT, "web", "templates", "billing_module.html"), encoding="utf-8").read()


def _fn(name):
    """A function's whole text from the module, by matching its braces."""
    i = SRC.index("function %s(" % name)
    depth, j = 0, SRC.index("{", i)
    while True:
        if SRC[j] == "{":
            depth += 1
        elif SRC[j] == "}":
            depth -= 1
            if depth == 0:
                return SRC[i:j + 1]
        j += 1


# Made-up figures: a half-hour of EV beside a battery, one with nothing priced yet.
CHART = {
    "currency": "£",
    "ti_cost": [0.2627, 0.0],
    "meters": {
        "ev": {"nice_name": "EV", "y": [2.293, 0.0], "cost": [0.12, 0.0], "has_rate": True, "is_export": False},
        "bat": {"nice_name": "Battery", "y": [2.728, 0.4], "cost": [0.1427, 0.0], "has_rate": True, "is_export": False},
        "exp": {"nice_name": "Grid Export", "y": [-0.5, 0.0], "cost": [0.0751, 0.0], "has_rate": True, "is_export": True},
        "plunge": {"nice_name": "Direct import", "y": [0.1, 0.0], "cost": [-0.004, 0.0], "has_rate": True,
                   "is_export": False},
    },
}
TOTAL = [5.121, 0.4]


def _labels():
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node not installed")
    js = _fn("hoverCost") + "\n" + _fn("hoverLabel") + "\n" + _fn("hoverFooter") + """
var d = %s, total = %s, out = {};
Object.keys(d.meters).forEach(function (k) {
  [0, 1].forEach(function (i) {
    out[k + i] = hoverLabel(d, {dataset: {_meter: d.meters[k]}, raw: {x: i, y: d.meters[k].y[i]}});
  });
});
var rate = {dataset: {_meter: d.meters.ev, _rate: true}, raw: {x: -0.5, y: 0.0523}};
out.rate = hoverLabel(d, rate);
[0, 1].forEach(function (i) {
  out['foot' + i] = hoverFooter(d, total, [rate, {dataset: {_meter: d.meters.ev}, raw: {x: i, y: 0}}]);
});
out.footRateOnly = hoverFooter(d, total, [rate]);
console.log(JSON.stringify(out));
""" % (json.dumps(CHART), json.dumps(TOTAL))
    return json.loads(subprocess.run([node, "-e", js], capture_output=True, text=True, check=True).stdout)


class TheHover(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.got = _labels()

    def test_a_bar_has_its_cost(self):
        self.assertEqual(self.got["ev0"], "EV: 2.293 kWh (£0.12)")
        self.assertEqual(self.got["bat0"], "Battery: 2.728 kWh (£0.14)")

    def test_the_last_line_is_the_half_hours_total_and_its_cost(self):
        self.assertEqual(self.got["foot0"], "Total import: 5.121 kWh (£0.26)")

    def test_no_cost_where_there_is_none(self):
        self.assertEqual(self.got["bat1"], "Battery: 0.400 kWh")
        self.assertEqual(self.got["foot1"], "Total import: 0.400 kWh")
        self.assertEqual(self.got["footRateOnly"], "")

    def test_export_has_its_cost_and_no_total(self):
        self.assertEqual(self.got["exp0"], "Grid Export: 0.500 kWh (£0.08)")

    def test_a_negative_cost_keeps_its_sign(self):
        self.assertEqual(self.got["plunge0"], "Direct import: 0.100 kWh (−£0.0040)")

    def test_GUARD_a_rate_is_unchanged(self):
        self.assertEqual(self.got["rate"], "EV rate: £0.0523")


if __name__ == "__main__":
    unittest.main()
