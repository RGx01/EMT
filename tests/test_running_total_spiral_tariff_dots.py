"""
The tariff dots on the spiral (1 Nov 2026, the user: "The dots for tariffs on the year by year spiral are
missing").

#396 put the tariffs on Year by year's line as dots and left the spiral without them. Now the spiral draws
them too, on both windings: a filled dot at each import agreement's start and a smaller hollow one at each
price change inside it, on the coil at the day's point (the last drawn on or before it in that year), in the
year's colour; none for a year not shown, a day before its year's data or after it. The hover is the bars'
tariff tip at the dot's day (its own, not the coil's day tip under it). The Tariffs box shows on every
Running total drawing and hides them.

Checked in the local preview on 1 Nov 2026, on a scratch copy, with price changes injected.

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
    line = PAGE[i:PAGE.index("\n", i)]
    if line.count("{") == line.count("}"):
        return line + "\n"
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


def _p(frm):
    return {"kind": "unit", "band": "flat", "from": frm, "inc": 20}


TARIFFS = [
    {"direction": "import", "name": "Go", "from": "2023-03-01", "to": "2024-02-01",
     "prices": [_p("2023-03-01"), _p("2023-10-01")]},
    {"direction": "import", "name": "Intelligent Go", "from": "2024-02-01", "to": None,
     "prices": [_p("2024-02-01"), _p("2024-06-01"), _p("2099-01-01")]},
    {"direction": "export", "name": "Out", "from": "2023-05-01", "to": None, "prices": []},
]


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheDots(unittest.TestCase):

    def _run(self, days, years=(2023, 2024)):
        # the coil's points: a day each, from `days` [first, last) as ISO dates
        js = ("var barAccStart = 'jan', barData = [], barBillingDay = 1, barEras = [], BAR_ACC_YC = ['#a', '#b'],"
              " BAR_ACC_MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];\n"
              "var barTariffs = %s;\n") % json.dumps(TARIFFS) + \
            "\n".join(_fn(n) for n in ("barAccMs", "barAccStartChoices", "barTariffPriceStarts", "barTariffChangeDays",
                                       "barAccDateLabel", "barAccAnchor", "barAccStartMs", "barAccYearOf",
                                       "barAccTariffDays", "barAccSpiralDots")) + \
            "\nfunction erasGroups() { return []; }\n" \
            "var pts = [], t = Date.parse(%s + 'T00:00:00Z'), end = Date.parse(%s + 'T00:00:00Z');\n" \
            "for (; t < end; t += 864e5) { var d = new Date(t); var r = {year: d.getUTCFullYear(), month: d.getUTCMonth() + 1, day: d.getUTCDate()};" \
            " pts.push({r: r, yr: barAccYearOf(r)}); }\n" % (json.dumps(days[0]), json.dumps(days[1])) + \
            "console.log(JSON.stringify(barAccSpiralDots(pts, %s).map(function (o) { " \
            "return [o.iso, o.change, o.color, barAccMs(o.p.r) === Date.parse(o.iso + 'T00:00:00Z')]; })));" % json.dumps(list(years))
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_a_dot_at_each_start_and_change_at_its_days_point(self):
        self.assertEqual(self._run(("2023-01-01", "2025-01-01")),
                         [["2023-03-01", False, "#a", True], ["2023-10-01", True, "#a", True],
                          ["2024-02-01", False, "#b", True], ["2024-06-01", True, "#b", True]])

    def test_none_outside_the_days_shown(self):
        self.assertEqual([d[0] for d in self._run(("2023-01-01", "2025-01-01"), years=(2024,))],
                         ["2024-02-01", "2024-06-01"])
        self.assertEqual([d[0] for d in self._run(("2023-04-01", "2024-03-01"))],     # 1 Mar 2023 before; Jun 2024 after
                         ["2023-10-01", "2024-02-01"])


class TheView(unittest.TestCase):

    def test_the_spiral_draws_them_with_the_bars_tip(self):
        f = _fn("barAccSpiral")
        self.assertIn("barAccSpiralDots(pts, years).forEach(function(t) {", f)
        self.assertIn("ev.stopPropagation(); tip.style.opacity = 0; barTariffShow(t.a, ev, t.iso);", f)

    def test_the_box_on_every_running_total_drawing(self):
        self.assertIn("vis('bar-tariff-wrap', !!(barTariffs && barTariffs.length), 'flex');", _fn("barAccSyncUI"))


if __name__ == "__main__":
    unittest.main()
